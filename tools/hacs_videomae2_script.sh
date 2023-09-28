#!/bin/bash

CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/hacs_videomae2.yaml --output pretrain --ckpt-freq 1
echo "start testing..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/hacs_videomae2.yaml ckpt/hacs_videomae2_pretrain/epoch_010.pth.tar

