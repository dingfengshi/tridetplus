#!/bin/bash

CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/hacs_videomae2_dino.yaml --output pretrain --ckpt-freq 1

echo "start testing..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/hacs_videomae2_dino.yaml ckpt/hacs_videomae2_dino_pretrain/epoch_009.pth.tar
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/hacs_videomae2_dino.yaml ckpt/hacs_videomae2_dino_pretrain/epoch_010.pth.tar
