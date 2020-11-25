from saliency_toolbox import calculate_measures

# https://github.com/Mehrdad-Noori/Saliency-Evaluation-Toolbox/blob/master/README.md

# Path to output foreground map folder
sm_dir = 'code/output /MINet_VGG16_S320_BS4_E50_WE1_AMPn_LR0.001_LTpoly_OTsgdtrick_ALy_BIy_MSn/pre/dut-omron'

# Path to corresponding ground truth mask folder
# May need some adjustments as Curtis's dataset folder structure doesn't work on my computer
gt_dir = 'MINet-Datasets/DUT-OMRON/Mask'


# At worst, we can manully get the results like this
# 'S-measure' is buggy at this moment
res    = calculate_measures(gt_dir, sm_dir, ['MAE', 'E-measure', 'S-measure', 'Max-F', 'Adp-F', 'Wgt-F'], 
save=False)

print(res)