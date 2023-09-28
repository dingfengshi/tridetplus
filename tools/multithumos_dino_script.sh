#!/bin/bash


CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/multithumos_dino.yaml --ckpt-freq 1 --output pretrain
echo "start testing..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/multithumos_dino.yaml ckpt/multithumos_dino_pretrain/epoch_047.pth.tar

