import os

__all__ = ["proj_root", "arg_config"]

from collections import OrderedDict

proj_root = os.path.dirname(__file__)
datasets_root = "../../MINet-Datasets"

ecssd_path = os.path.join(datasets_root, "ECSSD")
dutomron_path = os.path.join(datasets_root, "DUT-OMRON")
hkuis_path = os.path.join(datasets_root, "HKU-IS")
pascals_path = os.path.join(datasets_root, "PASCAL-S")
soc_path = os.path.join(datasets_root, "SOC/Validation")
soctr_path = os.path.join(datasets_root, "SOC/Train")
dutstr_path = os.path.join(datasets_root, "DUTS/Train")
dutste_path = os.path.join(datasets_root, "DUTS/Test")
msra10K_path = os.path.join(datasets_root, "MSRA10K")
thur15k_path = os.path.join(datasets_root, "THUR15K")
duts_soc_path = os.path.join(datasets_root, "DUTS-SOC")

arg_config = {
    "model": "MINet_VGG16",  # The netwotk model to be used. Need to import accordingly in 'network/__init__.py' ["MINet_Res50", "MINet_VGG16"]
    # "info": "SOCtr"
    "info": "",  # You can include supplmentary descriptions. It will be attached to the end of the exp_name. If left empty, nothing will be attached 
    "use_amp": False,  # Whether to enable AMP (Automatic Mixed Precision) to speed up training 
    "resume_mode": "test",  # The mode for resume parameters: ['train', 'test', 'measure', '']
                            # If resume_mode is 'measure' it will only generate new predictions if
                            # no predictions exist yet, otherwise it will simply compute the measure
                            # statistics on the existing predictions made with "save_pre": True
    "use_aux_loss": True,  # Whether to enable uxiliary loss. If true, will use CEL in the training
    "save_pre": True,  # Whether to save final prediction results
    "epoch_num": 50,  # Number of epochs. Set to 0 means to test the model directly
    "lr": 0.001,  # Learning rate. When fine-tuning, set to 1/100 of the original value
    "xlsx_name": "result_duts_train.xlsx",  # The name of the record file
    
    # Dataset settings
    "rgb_data": {
        "tr_data_path": dutstr_path,
        "te_data_list": OrderedDict(
            {
                # "pascal-s": pascals_path,
                # "ecssd": ecssd_path,
                # "hku-is": hkuis_path,
                # "duts": dutste_path,
                # "dut-omron": dutomron_path,
                # "soc": soc_path,
                # "msra10k": msra10K_path,
                "thur15k": thur15k_path,
            },
        ),
    },
    
    # Monitoring the training
    "tb_update": 50,  # if >0, will use tensorboard
    "print_freq": 50,  # >0, save iteration information
    # img_prefix, gt_prefix, the suffix of image files and mask files, respectively
    "prefix": (".jpg", ".png"),
    # if you don't want to use the multi-scale training, you can set 'size_list': None
    # "size_list": [224, 256, 288, 320, 352],
    "size_list": None,  # Not using multi-scale training
    "reduction": "mean",  # How to handle reduction,'mean' or 'sum'
    
    
    # Optimizer and the learning rate decay
    "optim": "sgd_trick",  # Customize the learning rate for part of the model
    "weight_decay": 5e-4,  # When fine-tuning, set to 0.0001
    "momentum": 0.9,
    "nesterov": False,
    "sche_usebatch": False,
    "lr_type": "poly",
    "warmup_epoch": 1,  # depond on the special lr_type, only lr_type has 'warmup', when set it to 1, it means no warmup.
    "lr_decay": 0.9,  # poly
    "use_bigt": True,  # In the training, whether to binarize the ground truth image (threshold = 0.5)
    "batch_size": 4,  # Keep the same batch_size when resuming a training
    "num_workers": 4,  # If too big, it will impact the speed of data reading
    "input_size": 320,
}
