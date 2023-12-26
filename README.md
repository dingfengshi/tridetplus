# Temporal Action Localization with Enhanced Instant Discriminability


## Overview

This repository contains the code for _Temporal Action Localization with Enhanced Instant Discriminability_ [paper](https://arxiv.org/abs/2309.05590). This code is extended upon the code of [TriDet](https://github.com/dingfengshi/TriDet).


## Installation

1. Please ensure that you have installed PyTorch and CUDA. **(This code requires PyTorch version >= 1.11. We use
   version=1.11.0 in our experiments)**

2. Install the required packages by running the following command:

```shell
pip install  -r requirements.txt
```

3. Install NMS

```shell
cd ./libs/utils
python setup.py install --user
cd ../..
```

## Data Preparation

### The VideoMAEv2 feature on the HACS dataset 
The pre-extracted features can be downloaded from this [Link](https://pan.baidu.com/s/1TFcKiFONAu9rKiavKj4Q_w?pwd=fqsy) (Password: fqsy). They are extracted with window size 16 and stride 8.

**Note**: Due to the large number of videos, it takes about a week to use 12 V100 GPUs to extract the features. To reduce extraction time, we used the VideoMAEv2 with half-precision weights to extract **some** features (obtained float16 features). Please convert the features to float32 when using:

```
feats = np.load(feature_path).astype(np.float32)
```

### The feature on the Charades dataset
We adopt the official RGB feature ([here](https://prior.allenai.org/projects/charades)) for our experiments. You can download it from [this link](https://ai2-public-datasets.s3-us-west-2.amazonaws.com/charades/Charades_v1_features_rgb.tar.gz)


**Note:** We provide the json file for Charades and multithumos in the ```data``` folder.

## References

If you find this work helpful, please consider citing our paper

```
@inproceedings{shi2023tridet,
  title={TriDet: Temporal Action Detection with Relative Boundary Modeling},
  author={Shi, Dingfeng and Zhong, Yujie and Cao, Qiong and Ma, Lin and Li, Jia and Tao, Dacheng},
  booktitle={Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition},
  pages={18857--18866},
  year={2023}
}
```
```
@article{shi2023temporal,
  title={Temporal Action Localization with Enhanced Instant Discriminability},
  author={Shi, Dingfeng and Cao, Qiong and Zhong, Yujie and An, Shan and Cheng, Jian and Zhu, Haogang and Tao, Dacheng},
  journal={arXiv preprint arXiv:2309.05590},
  year={2023}
}
```
 
