import os
from pprint import pprint

import numpy as np
import torch
from PIL import Image
from torchvision import transforms
from tqdm import tqdm
import skimage
import network as network_lib
from loss.CEL import CEL
from utils.dataloader import create_loader
from utils.metric import cal_maxf, cal_pr_mae_meanf
from measure.saliency_toolbox import  (
    read_and_normalize,
    mean_square_error,
    e_measure,
    s_measure,
    adaptive_fmeasure,
    weighted_fmeasure,
    prec_recall,
)
from utils.misc import (
    AvgMeter,
    construct_print,
    write_data_to_file,
)
from utils.pipeline_ops import (
    get_total_loss,
    make_optimizer,
    make_scheduler,
    resume_checkpoint,
    save_checkpoint,
)
from utils.recorder import TBRecorder, Timer, XLSXRecoder
from datetime import datetime

class Solver:
    def __init__(self, exp_name: str, arg_dict: dict, path_dict: dict):
        super(Solver, self).__init__()
        self.exp_name = exp_name
        self.arg_dict = arg_dict
        self.path_dict = path_dict

        self.dev = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.to_pil = transforms.ToPILImage()

        self.tr_data_path = self.arg_dict["rgb_data"]["tr_data_path"]
        self.te_data_list = self.arg_dict["rgb_data"]["te_data_list"]

        self.save_path = self.path_dict["save"]
        self.save_pre = self.arg_dict["save_pre"]

        if self.arg_dict["tb_update"] > 0:
            self.tb_recorder = TBRecorder(tb_path=self.path_dict["tb"])
        if self.arg_dict["xlsx_name"]:
            self.xlsx_recorder = XLSXRecoder(xlsx_path=self.path_dict["xlsx"],module_name=self.arg_dict["model"],model_name=self.exp_name)

        # 依赖与前面属性的属性
        self.tr_loader = create_loader(
            data_path=self.tr_data_path,
            training=True,
            size_list=self.arg_dict["size_list"],
            prefix=self.arg_dict["prefix"],
            get_length=False,
        )
        self.end_epoch = self.arg_dict["epoch_num"]
        self.iter_num = self.end_epoch * len(self.tr_loader)

        if hasattr(network_lib, self.arg_dict["model"]):
            self.net = getattr(network_lib, self.arg_dict["model"])().to(self.dev)
        else:
            raise AttributeError
        pprint(self.arg_dict)

        if self.arg_dict["resume_mode"] == "test" or self.arg_dict["resume_mode"] == "measure":
            # resume model only to test model.
            # self.start_epoch is useless
            resume_checkpoint(
                model=self.net, load_path=self.path_dict["final_state_net"], mode="onlynet",
            )
            return

        self.loss_funcs = [
            torch.nn.BCEWithLogitsLoss(reduction=self.arg_dict["reduction"]).to(self.dev)
        ]
        if self.arg_dict["use_aux_loss"]:
            self.loss_funcs.append(CEL().to(self.dev))

        self.opti = make_optimizer(
            model=self.net,
            optimizer_type=self.arg_dict["optim"],
            optimizer_info=dict(
                lr=self.arg_dict["lr"],
                momentum=self.arg_dict["momentum"],
                weight_decay=self.arg_dict["weight_decay"],
                nesterov=self.arg_dict["nesterov"],
            ),
        )
        self.sche = make_scheduler(
            optimizer=self.opti,
            total_num=self.iter_num if self.arg_dict["sche_usebatch"] else self.end_epoch,
            scheduler_type=self.arg_dict["lr_type"],
            scheduler_info=dict(
                lr_decay=self.arg_dict["lr_decay"], warmup_epoch=self.arg_dict["warmup_epoch"]
            ),
        )

        # AMP
        if self.arg_dict["use_amp"]:
            construct_print("Now, we will use the amp to accelerate training!")
            from apex import amp

            self.amp = amp
            self.net, self.opti = self.amp.initialize(self.net, self.opti, opt_level="O1")
        else:
            self.amp = None

        if self.arg_dict["resume_mode"] == "train":
            # resume model to train the model
            self.start_epoch = resume_checkpoint(
                model=self.net,
                optimizer=self.opti,
                scheduler=self.sche,
                amp=self.amp,
                exp_name=self.exp_name,
                load_path=self.path_dict["final_full_net"],
                mode="all",
            )
        else:
            # only train a new model.
            self.start_epoch = 0

    def train(self):
        for curr_epoch in range(self.start_epoch, self.end_epoch):
            train_loss_record = AvgMeter()
            self._train_per_epoch(curr_epoch, train_loss_record)

            # 根据周期修改学习率
            if not self.arg_dict["sche_usebatch"]:
                self.sche.step()

            # 每个周期都进行保存测试，保存的是针对第curr_epoch+1周期的参数
            save_checkpoint(
                model=self.net,
                optimizer=self.opti,
                scheduler=self.sche,
                amp=self.amp,
                exp_name=self.exp_name,
                current_epoch=curr_epoch + 1,
                full_net_path=self.path_dict["final_full_net"],
                state_net_path=self.path_dict["final_state_net"],
            )  # 保存参数

        if self.arg_dict["use_amp"]:
            # https://github.com/NVIDIA/apex/issues/567
            with self.amp.disable_casts():
                construct_print("When evaluating, we wish to evaluate in pure fp32.")
                self.test()
        else:
            self.test()

    @Timer
    def _train_per_epoch(self, curr_epoch, train_loss_record):
        for curr_iter_in_epoch, train_data in enumerate(self.tr_loader):
            num_iter_per_epoch = len(self.tr_loader)
            curr_iter = curr_epoch * num_iter_per_epoch + curr_iter_in_epoch

            self.opti.zero_grad()

            train_inputs, train_masks, _ = train_data
            train_inputs = train_inputs.to(self.dev, non_blocking=True)
            train_masks = train_masks.to(self.dev, non_blocking=True)
            train_preds = self.net(train_inputs)

            train_loss, loss_item_list = get_total_loss(train_preds, train_masks, self.loss_funcs)
            if self.amp:
                with self.amp.scale_loss(train_loss, self.opti) as scaled_loss:
                    scaled_loss.backward()
            else:
                train_loss.backward()
            self.opti.step()

            if self.arg_dict["sche_usebatch"]:
                self.sche.step()

            # 仅在累计的时候使用item()获取数据
            train_iter_loss = train_loss.item()
            train_batch_size = train_inputs.size(0)
            train_loss_record.update(train_iter_loss, train_batch_size)

            # 显示tensorboard
            if (
                self.arg_dict["tb_update"] > 0
                and (curr_iter + 1) % self.arg_dict["tb_update"] == 0
            ):
                self.tb_recorder.record_curve("trloss_avg", train_loss_record.avg, curr_iter)
                self.tb_recorder.record_curve("trloss_iter", train_iter_loss, curr_iter)
                self.tb_recorder.record_curve("lr", self.opti.param_groups, curr_iter)
                self.tb_recorder.record_image("trmasks", train_masks, curr_iter)
                self.tb_recorder.record_image("trsodout", train_preds.sigmoid(), curr_iter)
                self.tb_recorder.record_image("trsodin", train_inputs, curr_iter)
            # 记录每一次迭代的数据
            if (
                self.arg_dict["print_freq"] > 0
                and (curr_iter + 1) % self.arg_dict["print_freq"] == 0
            ):
                lr_str = ",".join(
                    [f"{param_groups['lr']:.7f}" for param_groups in self.opti.param_groups]
                )
                log = (
                    f"{curr_iter_in_epoch}:{num_iter_per_epoch}/"
                    f"{curr_iter}:{self.iter_num}/"
                    f"{curr_epoch}:{self.end_epoch} "
                    f"{self.exp_name}\n"
                    f"Lr:{lr_str} "
                    f"M:{train_loss_record.avg:.5f} C:{train_iter_loss:.5f} "
                    f"{loss_item_list}"
                )
                print(log)
                write_data_to_file(log, self.path_dict["tr_log"])

    def test(self):
        self.net.eval()

        msg = f"Testing start time: {datetime.now()}"
        construct_print(msg)
        write_data_to_file(msg, self.path_dict["te_log"])

        total_results = {}
        for data_name, data_path in self.te_data_list.items():
            construct_print(f"Testing with testset: {data_name}")
            self.te_loader = create_loader(
                data_path=data_path,
                training=False,
                prefix=self.arg_dict["prefix"],
                get_length=False,
            )
            self.save_path = os.path.join(self.path_dict["save"], data_name)
            if not os.path.exists(self.save_path):
                construct_print(f"{self.save_path} do not exist. Let's create it.")
                os.makedirs(self.save_path)
            results = self._test_process(save_pre=self.save_pre)
            msg = f"Results on the testset({data_name}:'{data_path}'): {results}"
            construct_print(msg)
            write_data_to_file(msg, self.path_dict["te_log"])
            # Print out time taken
            msg = f"Time Finish on testset {data_name}: {datetime.now()}"
            construct_print(msg)
            write_data_to_file(msg, self.path_dict["te_log"])

            total_results[data_name] = results

        self.net.train()

        if self.arg_dict["xlsx_name"]:
            # save result into xlsx file.
            self.xlsx_recorder.write_xlsx(self.exp_name, total_results)

    def _test_process(self, save_pre):
        loader = self.te_loader

        # pres = [AvgMeter() for _ in range(256)]
        # recs = [AvgMeter() for _ in range(256)]
        pres = list()
        recs = list()

        meanfs = AvgMeter()
        maes = AvgMeter()
        
        # Measures from Saliency toolbox
        measures = ['Wgt-F', 'E-measure', 'S-measure', 'Mod-Max-F', 'Mod-Adp-F', 'Mod-Wgt-F']
        beta=np.sqrt(0.3) # default beta parameter used in the adaptive F-measure
        gt_threshold=0.5 # The threshold that is used to binrize ground truth maps.

        values = dict() # initialize measure value dictionary
        pr = dict() # initialize precision recall dictionary
        prm = dict() # initialize precision recall dictionary for Mod-Max-F
        for idx in measures:
            values[idx] = list()
            if idx == 'Max-F':
                pr['Precision'] = list()
                pr['Recall']    = list()
            if idx == 'Mod-Max-F':
                prm['Precision'] = list()
                prm['Recall']    = list()

        tqdm_iter = tqdm(enumerate(loader), total=len(loader), leave=False)
        for test_batch_id, test_data in tqdm_iter:
            tqdm_iter.set_description(f"{self.exp_name}: te=>{test_batch_id + 1}")
            in_imgs, in_mask_paths, in_names = test_data

            generate_out_imgs = False
            if self.arg_dict["resume_mode"] == "measure":
                # Check if prediction masks have already been created
                for item_id, in_fname in enumerate(in_names):
                    oimg_path = os.path.join(self.save_path, in_fname + ".png")
                    if not os.path.exists(oimg_path):
                        # Out image doesn't exist yet
                        generate_out_imgs = True
                        break
            else:
                generate_out_imgs = True

            if generate_out_imgs:
                with torch.no_grad():
                    in_imgs = in_imgs.to(self.dev, non_blocking=True)
                    outputs = self.net(in_imgs)

                outputs_np = outputs.sigmoid().cpu().detach()

            for item_id, in_fname in enumerate(in_names):
                oimg_path = os.path.join(self.save_path, in_fname + ".png")
                gimg_path = os.path.join(in_mask_paths[item_id])
                gt_img = Image.open(gimg_path).convert("L")

                if self.arg_dict["resume_mode"] == "measure" and generate_out_imgs == False:
                    out_img = Image.open(oimg_path).convert("L")
                else:
                    out_item = outputs_np[item_id]
                    out_img = self.to_pil(out_item).resize(gt_img.size, resample=Image.NEAREST)

                if save_pre and generate_out_imgs:
                    out_img.save(oimg_path)

                gt_img = np.array(gt_img)
                out_img = np.array(out_img)

                # Gather images again using Saliency toolboxes import methods
                # These images will be grayscale floats between 0 and 1
                sm = out_img.astype(np.float32)
                if sm.max() == sm.min():
                    sm = sm / 255
                else:
                    sm = (sm - sm.min()) / (sm.max() - sm.min())
                gt = np.zeros_like(gt_img, dtype=np.float32)
                gt[gt_img > 256*gt_threshold] = 1

                ps, rs, mae, meanf = cal_pr_mae_meanf(out_img, gt_img)
                pres.append(ps)
                recs.append(rs)
                # for pidx, pdata in enumerate(zip(ps, rs)):
                #     p, r = pdata
                #     pres[pidx].update(p)
                #     recs[pidx].update(r)
                maes.update(mae)
                meanfs.update(meanf)

                # Compute other measures using the Saliency Toolbox
                if 'MAE2' in measures:
                    values['MAE2'].append(mean_square_error(gt, sm))
                if 'E-measure' in measures:
                    values['E-measure'].append(e_measure(gt, sm))
                if 'S-measure' in measures:
                    values['S-measure'].append(s_measure(gt, sm))
                if 'Adp-F' in measures:
                    values['Adp-F'].append(adaptive_fmeasure(gt, sm, beta, allowBlackMask=False))
                if 'Mod-Adp-F' in measures:
                    values['Mod-Adp-F'].append(adaptive_fmeasure(gt, sm, beta, allowBlackMask=True))
                if 'Wgt-F' in measures:
                    values['Wgt-F'].append(weighted_fmeasure(gt, sm, allowBlackMask=False))
                if 'Mod-Wgt-F' in measures:
                    values['Mod-Wgt-F'].append(weighted_fmeasure(gt, sm, allowBlackMask=True))
                if 'Max-F' in measures:
                    prec, recall = prec_recall(gt, sm, 256, allowBlackMask=False)  # 256 thresholds between 0 and 1

                    # Check if precision recall curve exists
                    if len(prec) != 0 and len(recall) != 0:
                        pr['Precision'].append(prec)
                        pr['Recall'].append(recall)
                if 'Mod-Max-F' in measures:
                    prec, recall = prec_recall(gt, sm, 256, allowBlackMask=True)  # 256 thresholds between 0 and 1

                    # Check if precision recall curve exists
                    if len(prec) != 0 and len(recall) != 0:
                        prm['Precision'].append(prec)
                        prm['Recall'].append(recall)

        # Compute total measures over all images
        if 'MAE2' in measures:
            values['MAE2'] = np.mean(values['MAE2'])

        if 'E-measure' in measures:
            values['E-measure'] = np.mean(values['E-measure'])

        if 'S-measure' in measures:
            values['S-measure'] = np.mean(values['S-measure'])

        if 'Adp-F' in measures:
            values['Adp-F'] = np.mean(values['Adp-F'])
        if 'Mod-Adp-F' in measures:
            values['Mod-Adp-F'] = np.mean(values['Mod-Adp-F'])

        if 'Wgt-F' in measures:
            values['Wgt-F'] = np.mean(values['Wgt-F'])
        if 'Mod-Wgt-F' in measures:
            values['Mod-Wgt-F'] = np.mean(values['Mod-Wgt-F'])

        if 'Max-F' in measures:
            if len(pr['Precision']) > 0:
                pr['Precision'] = np.mean(np.hstack(pr['Precision'][:]), 1)
                pr['Recall'] = np.mean(np.hstack(pr['Recall'][:]), 1)
                f_measures = (1 + beta ** 2) * pr['Precision'] * pr['Recall'] / (
                        beta ** 2 * pr['Precision'] + pr['Recall'])

                # Remove any NaN values to allow calculation
                f_measures[np.isnan(f_measures)] = 0
                values['Max-F'] = np.max(f_measures)
            else:
                # There were likely no images found in the directory, so pr['Precision']
                # is an empty set
                values['Max-F'] = 0
        if 'Mod-Max-F' in measures:
            if len(prm['Precision']) > 0:
                prm['Precision'] = np.mean(np.hstack(prm['Precision'][:]), 1)
                prm['Recall'] = np.mean(np.hstack(prm['Recall'][:]), 1)
                f_measures = (1 + beta ** 2) * prm['Precision'] * prm['Recall'] / (
                        beta ** 2 * prm['Precision'] + prm['Recall'])

                # Remove any NaN values to allow calculation
                f_measures[np.isnan(f_measures)] = 0
                values['Mod-Max-F'] = np.max(f_measures)
            else:
                # There were likely no images found in the directory, so prm['Precision']
                # is an empty set
                values['Mod-Max-F'] = 0

        # maxf = cal_maxf([pre.avg for pre in pres], [rec.avg for rec in recs])

        # Calculate MAXF using original algorithm pr, re curves
        pres = np.mean(np.hstack(pres[:]), 1)
        recs = np.mean(np.hstack(recs[:]), 1)
        f_measures = (1 + beta ** 2) * pres * recs / (
                beta ** 2 * pres + recs)
        # Remove any NaN values to allow calculation
        f_measures[np.isnan(f_measures)] = 0
        maxf = np.max(f_measures)

        results = {"MAXF": maxf, "MEANF": meanfs.avg, "MAE": maes.avg, **values}
        return results
