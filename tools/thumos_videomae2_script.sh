#!/bin/bash

CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/thumos_videomae2.yaml --output pretrain --ckpt-freq 1
echo "start testing..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/thumos_videomae2.yaml ckpt/thumos_videomae2_pretrain/epoch_039.pth.tar
