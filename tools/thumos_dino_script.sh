#!/bin/bash

CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/thumos_videomae2_dino.yaml --output pretrain --ckpt-freq 1
echo "start testing..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/thumos_videomae2_dino.yaml ckpt/thumos_videomae2_dino_pretrain/epoch_039.pth.tar
