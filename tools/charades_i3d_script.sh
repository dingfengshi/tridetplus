#!/bin/bash

CUDA_VISIBLE_DEVICES=$1 python train.py ./configs/charades_i3d.yaml --ckpt-freq 2 --output pretrain
echo "start testing14..."
CUDA_VISIBLE_DEVICES=$1 python eval.py ./configs/charades_i3d.yaml ckpt/charades_i3d_pretrain/epoch_008.pth.tar

