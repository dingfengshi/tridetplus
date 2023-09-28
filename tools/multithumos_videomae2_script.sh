#!/bin/bash


CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/multithumos_videomae2.yaml --ckpt-freq 1 --output pretrain
echo "start testing..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/multithumos_videomae2.yaml ckpt/multithumos_videomae2_pretrain/epoch_047.pth.tar

