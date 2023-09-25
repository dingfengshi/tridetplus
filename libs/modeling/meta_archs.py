import math
import torch
from torch import nn
from torch.nn import functional as F
from torch.nn.utils.rnn import pad_sequence

from .blocks import MaskedConv1D, Scale, LayerNorm
from .losses import ctr_diou_loss_1d, sigmoid_focal_loss, ctr_giou_loss_1d
from .models import register_meta_arch, make_backbone, make_neck, make_generator
from ..utils import batched_nms


class ClsHead(nn.Module):
    """
    1D Conv heads for classification
    """

    def __init__(
            self,
            input_dim,
            feat_dim,
            num_classes,
            prior_prob=0.01,
            num_layers=3,
            kernel_size=3,
            act_layer=nn.ReLU,
            with_ln=False,
            empty_cls=[],
            detach_feat=False
    ):
        super().__init__()
        self.act = act_layer()
        self.detach_feat = detach_feat

        # build the head
        self.head = nn.ModuleList()
        self.norm = nn.ModuleList()
        for idx in range(num_layers - 1):
            if idx == 0:
                in_dim = input_dim
                out_dim = feat_dim
            else:
                in_dim = feat_dim
                out_dim = feat_dim
            self.head.append(
                MaskedConv1D(
                    in_dim, out_dim, kernel_size,
                    stride=1,
                    padding=kernel_size // 2,
                    bias=(not with_ln)
                )
            )
            if with_ln:
                self.norm.append(
                    LayerNorm(out_dim)
                )
            else:
                self.norm.append(nn.Identity())

        # classifier
        self.cls_head = MaskedConv1D(
            feat_dim, num_classes, kernel_size,
            stride=1, padding=kernel_size // 2
        )

        # use prior in model initialization to improve stability
        # this will overwrite other weight init
        bias_value = -(math.log((1 - prior_prob) / prior_prob))
        torch.nn.init.constant_(self.cls_head.conv.bias, bias_value)

        # a quick fix to empty categories:
        # the weights assocaited with these categories will remain unchanged
        # we set their bias to a large negative value to prevent their outputs
        if len(empty_cls) > 0:
            bias_value = -(math.log((1 - 1e-6) / 1e-6))
            for idx in empty_cls:
                torch.nn.init.constant_(self.cls_head.conv.bias[idx], bias_value)

    def forward(self, fpn_feats, fpn_masks):
        assert len(fpn_feats) == len(fpn_masks)

        # apply the classifier for each pyramid level
        out_logits = tuple()
        for _, (cur_feat, cur_mask) in enumerate(zip(fpn_feats, fpn_masks)):
            if self.detach_feat:
                cur_out = cur_feat.detach()
            else:
                cur_out = cur_feat
            for idx in range(len(self.head)):
                cur_out, _ = self.head[idx](cur_out, cur_mask)
                cur_out = self.act(self.norm[idx](cur_out))
            cur_logits, _ = self.cls_head(cur_out, cur_mask)
            out_logits += (cur_logits,)

        # fpn_masks remains the same
        return out_logits


class RegHead(nn.Module):
    """
    Shared 1D Conv heads for regression
    Simlar logic as PtTransformerClsHead with separated implementation for clarity
    """

    def __init__(
            self,
            input_dim,
            feat_dim,
            fpn_levels,
            num_layers=3,
            kernel_size=3,
            act_layer=nn.ReLU,
            with_ln=False,
            num_bins=16
    ):
        super().__init__()
        self.fpn_levels = fpn_levels
        self.act = act_layer()

        # build the conv head
        self.head = nn.ModuleList()
        self.norm = nn.ModuleList()
        for idx in range(num_layers - 1):
            if idx == 0:
                in_dim = input_dim
                out_dim = feat_dim
            else:
                in_dim = feat_dim
                out_dim = feat_dim
            self.head.append(
                MaskedConv1D(
                    in_dim, out_dim, kernel_size,
                    stride=1,
                    padding=kernel_size // 2,
                    bias=(not with_ln)
                )
            )
            if with_ln:
                self.norm.append(
                    LayerNorm(out_dim)
                )
            else:
                self.norm.append(nn.Identity())

        self.scale = nn.ModuleList()
        for idx in range(fpn_levels):
            self.scale.append(Scale())

        self.offset_head = MaskedConv1D(
            feat_dim, 2 * (num_bins + 1), kernel_size,
            stride=1, padding=kernel_size // 2
        )

    def forward(self, fpn_feats, fpn_masks):
        assert len(fpn_feats) == len(fpn_masks)
        assert len(fpn_feats) == self.fpn_levels

        # apply the classifier for each pyramid level
        out_offsets = tuple()
        for l, (cur_feat, cur_mask) in enumerate(zip(fpn_feats, fpn_masks)):
            cur_out = cur_feat
            for idx in range(len(self.head)):
                cur_out, _ = self.head[idx](cur_out, cur_mask)
                cur_out = self.act(self.norm[idx](cur_out))
            cur_offsets, _ = self.offset_head(cur_out, cur_mask)
            out_offsets += (F.relu(self.scale[l](cur_offsets)),)

        # fpn_masks remains the same
        return out_offsets


@register_meta_arch("TriDet")
class TriDet(nn.Module):
    """
        Transformer based model for single stage action localization
    """

    def __init__(
            self,
            backbone_type,  # a string defines which backbone we use
            fpn_type,  # a string defines which fpn we use
            backbone_arch,  # a tuple defines # layers in embed / stem / branch
            scale_factor,  # scale factor between branch layers
            input_dim,  # input feat dim
            max_seq_len,  # max sequence length (used for training)
            max_buffer_len_factor,  # max buffer size (defined a factor of max_seq_len)
            n_sgp_win_size,  # window size w for sgp
            embd_kernel_size,  # kernel size of the embedding network
            embd_dim,  # output feat channel of the embedding network
            embd_with_ln,  # attach layernorm to embedding network
            fpn_dim,  # feature dim on FPN,
            sgp_mlp_dim,  # the numnber of dim in SGP
            fpn_with_ln,  # if to apply layer norm at the end of fpn
            head_dim,  # feature dim for head
            regression_range,  # regression range on each level of FPN
            head_num_layers,  # number of layers in the head (including the classifier)
            head_kernel_size,  # kernel size for reg/cls heads
            boudary_kernel_size,  # kernel size for boundary heads
            head_with_ln,  # attache layernorm to reg/cls heads
            use_abs_pe,  # if to use abs position encoding
            num_bins,  # the bin number in Trident-head (exclude 0)
            iou_weight_power,  # the power of iou weight in loss
            downsample_type,  # how to downsample feature in FPN
            input_noise,  # add gaussian noise with the variance, play a similar role to position embedding
            k,  # the K in SGP
            init_conv_vars,  # initialization of gaussian variance for the weight in SGP
            use_trident_head,  # if use the Trident-head
            num_classes,  # number of action classes
            train_cfg,  # other cfg for training
            test_cfg,  # other cfg for testing
            multi_label,
            additional_fature=False,
            additional_dim=-1,
            additional_only=False
    ):
        super().__init__()
        # re-distribute params to backbone / neck / head
        self.fpn_strides = [scale_factor ** i for i in range(backbone_arch[-1] + 1)]

        self.input_noise = input_noise

        self.reg_range = regression_range
        assert len(self.fpn_strides) == len(self.reg_range)
        self.scale_factor = scale_factor
        self.iou_weight_power = iou_weight_power
        # #classes = num_classes + 1 (background) with last category as background
        # e.g., num_classes = 10 -> 0, 1, ..., 9 as actions, 10 as background
        self.num_classes = num_classes
        self.multi_label = multi_label

        # check the feature pyramid and local attention window size
        self.max_seq_len = max_seq_len
        if isinstance(n_sgp_win_size, int):
            self.sgp_win_size = [n_sgp_win_size] * len(self.fpn_strides)
        else:
            assert len(n_sgp_win_size) == len(self.fpn_strides)
            self.sgp_win_size = n_sgp_win_size
        max_div_factor = 1
        for l, (s, w) in enumerate(zip(self.fpn_strides, self.sgp_win_size)):
            stride = s * w if w > 1 else s
            if max_div_factor < stride:
                max_div_factor = stride
        self.max_div_factor = max_div_factor

        # training time config
        self.train_center_sample = train_cfg['center_sample']
        assert self.train_center_sample in ['radius', 'none']
        self.train_center_sample_radius = train_cfg['center_sample_radius']
        self.train_loss_weight = train_cfg['loss_weight']
        self.train_cls_prior_prob = train_cfg['cls_prior_prob']
        self.train_dropout = train_cfg['dropout']
        self.train_droppath = train_cfg['droppath']
        self.train_label_smoothing = train_cfg['label_smoothing']

        # test time config
        self.test_pre_nms_thresh = test_cfg['pre_nms_thresh']
        self.test_pre_nms_topk = test_cfg['pre_nms_topk']
        self.test_iou_threshold = test_cfg['iou_threshold']
        self.test_min_score = test_cfg['min_score']
        self.test_max_seg_num = test_cfg['max_seg_num']
        self.test_nms_method = test_cfg['nms_method']
        assert self.test_nms_method in ['soft', 'hard', 'none']
        self.test_duration_thresh = test_cfg['duration_thresh']
        self.test_multiclass_nms = test_cfg['multiclass_nms']
        self.test_nms_sigma = test_cfg['nms_sigma']
        self.test_voting_thresh = test_cfg['voting_thresh']
        self.num_bins = num_bins
        self.use_trident_head = use_trident_head
        self.additional_fature = additional_fature
        self.additional_only = additional_only

        # we will need a better way to dispatch the params to backbones / necks
        # backbone network: conv + transformer
        assert backbone_type in ['SGP', 'conv']
        if backbone_type == 'SGP':
            self.backbone = make_backbone(
                'SGP',
                **{
                    'n_in': input_dim,
                    'n_embd': embd_dim,
                    'sgp_mlp_dim': sgp_mlp_dim,
                    'n_embd_ks': embd_kernel_size,
                    'max_len': max_seq_len,
                    'arch': backbone_arch,
                    'scale_factor': scale_factor,
                    'with_ln': embd_with_ln,
                    'path_pdrop': self.train_droppath,
                    'downsample_type': downsample_type,
                    'sgp_win_size': self.sgp_win_size,
                    'use_abs_pe': use_abs_pe,
                    'k': k,
                    'init_conv_vars': init_conv_vars,
                    'additional_fature': self.additional_fature
                }
            )
        else:
            self.backbone = make_backbone(
                'conv',
                **{
                    'n_in': input_dim,
                    'n_embd': embd_dim,
                    'n_embd_ks': embd_kernel_size,
                    'arch': backbone_arch,
                    'scale_factor': scale_factor,
                    'with_ln': embd_with_ln,
                }
            )

        # fpn network: convs
        assert fpn_type in ['fpn', 'identity']
        self.neck = make_neck(
            fpn_type,
            **{
                'in_channels': [embd_dim] * (backbone_arch[-1] + 1),
                'out_channel': fpn_dim,
                'scale_factor': scale_factor,
                'with_ln': fpn_with_ln
            }
        )

        # location generator: points
        self.point_generator = make_generator(
            'point',
            **{
                'max_seq_len': max_seq_len * max_buffer_len_factor,
                'fpn_levels': len(self.fpn_strides),
                'scale_factor': scale_factor,
                'regression_range': self.reg_range,
                'strides': self.fpn_strides
            }
        )

        # classfication and regerssion heads
        self.cls_head = ClsHead(
            fpn_dim, head_dim, self.num_classes,
            kernel_size=head_kernel_size,
            prior_prob=self.train_cls_prior_prob,
            with_ln=head_with_ln,
            num_layers=head_num_layers,
            empty_cls=train_cfg['head_empty_cls']
        )

        if use_trident_head:
            if self.multi_label:
                model_bins = (num_bins + 1) * self.num_classes - 1
            else:
                model_bins = num_bins

            self.start_head = ClsHead(
                fpn_dim, head_dim, self.num_classes,
                kernel_size=boudary_kernel_size,
                prior_prob=self.train_cls_prior_prob,
                with_ln=head_with_ln,
                num_layers=head_num_layers,
                empty_cls=train_cfg['head_empty_cls'],
                detach_feat=True
            )
            self.end_head = ClsHead(
                fpn_dim, head_dim, self.num_classes,
                kernel_size=boudary_kernel_size,
                prior_prob=self.train_cls_prior_prob,
                with_ln=head_with_ln,
                num_layers=head_num_layers,
                empty_cls=train_cfg['head_empty_cls'],
                detach_feat=True
            )

            self.reg_head = RegHead(
                fpn_dim, head_dim, len(self.fpn_strides),
                kernel_size=head_kernel_size,
                num_layers=head_num_layers,
                with_ln=head_with_ln,
                num_bins=model_bins
            )
        else:
            if self.multi_label:
                model_bins = self.num_classes - 1
            else:
                model_bins = 0
            self.reg_head = RegHead(
                fpn_dim, head_dim, len(self.fpn_strides),
                kernel_size=head_kernel_size,
                num_layers=head_num_layers,
                with_ln=head_with_ln,
                num_bins=model_bins
            )

        if self.additional_fature:
            self.pose_conv = False
            if self.pose_conv:
                # bz*T, cls, height, width
                self.additional_embed = nn.Sequential(
                    nn.Conv2d(7,
                              embd_dim,
                              kernel_size=(56, 56),
                              stride=(1, 1),
                              padding=(0, 0),
                              bias=True,
                              ),
                    nn.GroupNorm(16, embd_dim),
                    nn.ReLU(),
                    nn.Conv2d(embd_dim,
                              embd_dim,
                              kernel_size=(1, 1),
                              stride=(1, 1),
                              padding=(0, 0),
                              bias=True,
                              ),
                    nn.GroupNorm(16, embd_dim),
                    nn.ReLU(),
                    nn.Conv2d(embd_dim,
                              embd_dim,
                              kernel_size=(1, 1),
                              stride=(1, 1),
                              padding=(0, 0),
                              bias=True,
                              ),
                    nn.GroupNorm(16, embd_dim),
                    nn.ReLU(),
                )

                self.pose_temporal = nn.Sequential(
                    nn.Conv1d(embd_dim, embd_dim, 3, 1, 1
                              ),
                    nn.ReLU()
                )

            else:
                self.additional_embed = nn.Sequential(
                    nn.Conv1d(additional_dim, embd_dim, kernel_size=1),
                    nn.GroupNorm(16, embd_dim),
                    nn.ReLU(),
                    nn.Conv1d(embd_dim, embd_dim, kernel_size=1),
                    nn.GroupNorm(16, embd_dim),
                    nn.ReLU()
                )


        # maintain an EMA of #foreground to stabilize the loss normalizer
        # useful for small mini-batch training
        self.loss_normalizer = train_cfg['init_loss_norm']
        self.loss_normalizer_momentum = 0.9

    @property
    def device(self):
        # a hacky way to get the device type
        # will throw an error if parameters are on different devices
        return list(set(p.device for p in self.parameters()))[0]

    def decode_offset(self, out_offsets, pred_left, pred_right):
        if not self.use_trident_head:
            if self.training:
                out_offsets = torch.cat(out_offsets, dim=1)
            if self.multi_label:
                out_offsets = out_offsets.reshape(out_offsets.shape[:-1] + (self.num_classes, -1))
            return out_offsets

        else:
            # Make an adaption for train and validation, when training, the out_offsets is a list with feature outputs
            # from each FPN level. Each feature with shape [batchsize, T_level, (Num_bin+1)x2].
            # For validation, the out_offsets is a feature with shape [T_level, (Num_bin+1)x2]
            if self.training:
                pred_left = torch.cat(pred_left, dim=1)
                pred_right = torch.cat(pred_right, dim=1)
                if self.multi_label:
                    out_offsets = torch.cat(out_offsets, dim=1)
                    out_offsets = out_offsets.view(out_offsets.shape[:2] + (2, self.num_classes, -1))
                    pred_left_dis = torch.softmax(pred_left + out_offsets[:, :, 0, :], dim=-1)
                    pred_right_dis = torch.softmax(pred_right + out_offsets[:, :, 1, :], dim=-1)
                else:
                    out_offsets = torch.cat(out_offsets, dim=1)
                    out_offsets = out_offsets.view(out_offsets.shape[:2] + (2, -1))
                    pred_left_dis = torch.softmax(pred_left + out_offsets[:, :, :1, :], dim=-1)
                    pred_right_dis = torch.softmax(pred_right + out_offsets[:, :, 1:, :], dim=-1)
            else:
                if self.multi_label:
                    out_offsets = out_offsets.view(out_offsets.shape[0], 2, self.num_classes, -1)
                    pred_left_dis = torch.softmax(pred_left + out_offsets[:, 0, :], dim=-1)
                    pred_right_dis = torch.softmax(pred_right + out_offsets[:, 1, :], dim=-1)
                else:
                    out_offsets = out_offsets.view(out_offsets.shape[0], 2, -1)
                    pred_left_dis = torch.softmax(pred_left + out_offsets[None, :, 0, :], dim=-1)
                    pred_right_dis = torch.softmax(pred_right + out_offsets[None, :, 1, :], dim=-1)

            max_range_num = pred_left_dis.shape[-1]

            left_range_idx = torch.arange(max_range_num - 1, -1, -1, device=pred_left.device,
                                          dtype=torch.float).unsqueeze(-1)
            right_range_idx = torch.arange(max_range_num, device=pred_right.device, dtype=torch.float).unsqueeze(-1)

            pred_left_dis = pred_left_dis.masked_fill(torch.isnan(pred_right_dis), 0)
            pred_right_dis = pred_right_dis.masked_fill(torch.isnan(pred_right_dis), 0)

            # calculate the value of expectation for the offset:
            decoded_offset_left = torch.matmul(pred_left_dis, left_range_idx)
            decoded_offset_right = torch.matmul(pred_right_dis, right_range_idx)
            return torch.cat([decoded_offset_left, decoded_offset_right], dim=-1)

    def forward(self, video_list):
        # drop the data without annotation first
        video_list = [each for each in video_list if each['segments'] is not None]

        # batch the video list into feats (B, C, T) and masks (B, 1, T)
        batched_inputs, batched_masks, batched_additional_feats = self.preprocessing(video_list)

        if self.additional_fature:
            batched_additional_feats = self.additional_embed(batched_additional_feats)
        else:
            batched_additional_feats = None

        # forward the network (backbone -> neck -> heads)
        batched_inputs, batched_masks, batched_add_feats = self.backbone(batched_inputs, batched_masks,
                                                                         batched_additional_feats, self.additional_only)

        fpn_feats, fpn_masks = self.neck(batched_inputs, batched_masks)


        # compute the point coordinate along the FPN
        # this is used for computing the GT or decode the final results
        # points: List[T x 4] with length = # fpn levels
        # (shared across all samples in the mini-batch)
        points = self.point_generator(fpn_feats)

        # out_cls: List[B, #cls + 1, T_i]
        out_cls_logits = self.cls_head(fpn_feats, fpn_masks)

        if self.additional_fature and not self.additional_only:
            fpn_feats = [feat + add_feat for feat, add_feat in zip(fpn_feats, batched_add_feats)]

        if self.use_trident_head:
            out_lb_logits = self.start_head(fpn_feats, fpn_masks)
            out_rb_logits = self.end_head(fpn_feats, fpn_masks)
        else:
            out_lb_logits = None
            out_rb_logits = None

        # out_offset: List[B, 2, T_i]
        out_offsets = self.reg_head(fpn_feats, fpn_masks)

        # permute the outputs
        # out_cls: F List[B, #cls, T_i] -> F List[B, T_i, #cls]
        out_cls_logits = [x.permute(0, 2, 1) for x in out_cls_logits]
        # out_offset: F List[B, 2 (xC), T_i] -> F List[B, T_i, 2 (xC)]
        out_offsets = [x.permute(0, 2, 1) for x in out_offsets]
        # fpn_masks: F list[B, 1, T_i] -> F List[B, T_i]
        fpn_masks = [x.squeeze(1) for x in fpn_masks]

        # return loss during training
        if self.training:
            # generate segment/lable List[N x 2] / List[N] with length = B
            assert video_list[0]['segments'] is not None, "GT action labels does not exist"
            assert video_list[0]['labels'] is not None, "GT action labels does not exist"
            gt_segments = [x['segments'].to(self.device) for x in video_list]
            gt_labels = [x['labels'].to(self.device) for x in video_list]

            # compute the gt labels for cls & reg
            # list of prediction targets
            gt_cls_labels, gt_offsets = self.label_points(
                points, gt_segments, gt_labels)

            # compute the loss and return
            losses = self.losses(
                fpn_masks,
                out_cls_logits, out_offsets,
                gt_cls_labels, gt_offsets,
                out_lb_logits, out_rb_logits,
            )
            return losses

        else:
            # decode the actions (sigmoid / stride, etc)
            results = self.inference(
                video_list, points, fpn_masks,
                out_cls_logits, out_offsets,
                out_lb_logits, out_rb_logits,
            )
            return results

    @torch.no_grad()
    def preprocessing(self, video_list, padding_val=0.0):
        """
            Generate batched features and masks from a list of dict items
        """
        feats = [x['feats'] for x in video_list]
        feats_lens = torch.as_tensor([feat.shape[-1] for feat in feats], device=feats[0].device)
        max_len = feats_lens.max(0).values.item()

        if self.additional_fature:
            additional_feats = [x['additional_feats'] for x in video_list]
            additional_frames_num = [x.shape[-1] for x in additional_feats]

            additional_frames_num = torch.as_tensor(additional_frames_num)

        if self.training:
            assert max_len <= self.max_seq_len, "Input length must be smaller than max_seq_len during training"
            # set max_len to self.max_seq_len
            max_len = self.max_seq_len
            # batch input shape B, C, T
            batch_shape = [len(feats), feats[0].shape[0], max_len]
            batched_inputs = feats[0].new_full(batch_shape, padding_val)

            if self.additional_fature:
                batch_add_shape = (len(additional_feats), additional_feats[0].shape[0], max_len)
                batched_addfeat = additional_feats[0].new_full(batch_add_shape, 0.)

                for feat, pad_feat, add_feat, pose_pad_feat in zip(feats, batched_inputs, additional_feats,
                                                                   batched_addfeat):
                    pad_feat[..., :feat.shape[-1]].copy_(feat)
                    pose_pad_feat[..., :add_feat.shape[1]].copy_(add_feat)
            else:
                for feat, pad_feat in zip(feats, batched_inputs):
                    pad_feat[..., :feat.shape[-1]].copy_(feat)

            if self.input_noise > 0:
                noise = torch.randn_like(batched_inputs) * self.input_noise
                batched_inputs += noise
        else:
            assert len(video_list) == 1, "Only support batch_size = 1 during inference"
            # input length < self.max_seq_len, pad to max_seq_len
            if max_len <= self.max_seq_len:
                max_len = self.max_seq_len
            else:
                # pad the input to the next divisible size
                stride = self.max_div_factor
                max_len = (max_len + (stride - 1)) // stride * stride
            padding_size = [0, max_len - feats_lens[0]]
            batched_inputs = F.pad(feats[0], padding_size, value=padding_val).unsqueeze(0)

            if self.additional_fature:
                pose_padding_size = [0, max_len - additional_frames_num[0]]
                batched_addfeat = F.pad(additional_feats[0], pose_padding_size, value=padding_val).unsqueeze(0)

        # generate the mask
        batched_masks = torch.arange(max_len, device=batched_inputs.device)[None, :] < feats_lens[:, None]

        # push to device
        batched_masks = batched_masks.unsqueeze(1)

        if not self.additional_fature:
            batched_addfeat = None

        return batched_inputs, batched_masks, batched_addfeat

    @torch.no_grad()
    def label_points(self, points, gt_segments, gt_labels):
        # concat points on all fpn levels List[T x 4] -> F T x 4
        # This is shared for all samples in the mini-batch
        num_levels = len(points)
        concat_points = torch.cat(points, dim=0)
        gt_cls, gt_offset = [], []

        # loop over each video sample
        for gt_segment, gt_label in zip(gt_segments, gt_labels):
            cls_targets, reg_targets = self.label_points_single_video(
                concat_points, gt_segment, gt_label
            )
            # append to list (len = # images, each of size FT x C)
            gt_cls.append(cls_targets)
            gt_offset.append(reg_targets)

        return gt_cls, gt_offset

    @torch.no_grad()
    def label_points_single_video(self, concat_points, gt_segment, gt_label):
        # concat_points : F T x 4 (t, regressoin range, stride)
        # gt_segment : N (#Events) x 2
        # gt_label : N (#Events) x 1
        num_pts = concat_points.shape[0]
        num_gts = gt_segment.shape[0]

        # corner case where current sample does not have actions
        if num_gts == 0:
            cls_targets = gt_segment.new_full((num_pts, self.num_classes), 0)
            reg_targets = gt_segment.new_zeros((num_pts, 2))
            return cls_targets, reg_targets

        # compute the lengths of all segments -> F T x N
        lens = gt_segment[:, 1] - gt_segment[:, 0]
        lens = lens[None, :].repeat(num_pts, 1)

        # compute the distance of every point to each segment boundary
        # auto broadcasting for all reg target-> F T x N x2
        gt_segs = gt_segment[None].expand(num_pts, num_gts, 2)
        left = concat_points[:, 0, None] - gt_segs[:, :, 0]
        right = gt_segs[:, :, 1] - concat_points[:, 0, None]
        reg_targets = torch.stack((left, right), dim=-1)

        if self.train_center_sample == 'radius':
            # center of all segments F T x N
            center_pts = 0.5 * (gt_segs[:, :, 0] + gt_segs[:, :, 1])
            # center sampling based on stride radius
            # compute the new boundaries:
            # concat_points[:, 3] stores the stride
            t_mins = \
                center_pts - concat_points[:, 3, None] * self.train_center_sample_radius
            t_maxs = \
                center_pts + concat_points[:, 3, None] * self.train_center_sample_radius
            # prevent t_mins / maxs from over-running the action boundary
            # left: torch.maximum(t_mins, gt_segs[:, :, 0])
            # right: torch.minimum(t_maxs, gt_segs[:, :, 1])
            # F T x N (distance to the new boundary)
            cb_dist_left = concat_points[:, 0, None] - torch.maximum(t_mins, gt_segs[:, :, 0])
            cb_dist_right = torch.minimum(t_maxs, gt_segs[:, :, 1]) - concat_points[:, 0, None]
            # F T x N x 2
            center_seg = torch.stack((cb_dist_left, cb_dist_right), -1)
            # F T x N
            inside_gt_seg_mask = center_seg.min(-1)[0] > 0

        else:
            # inside an gt action
            inside_gt_seg_mask = reg_targets.min(-1)[0] > 0

        # limit the regression range for each location
        max_regress_distance = reg_targets.max(-1)[0]
        # F T x N
        inside_regress_range = torch.logical_and(
            (max_regress_distance >= concat_points[:, 1, None]),
            (max_regress_distance <= concat_points[:, 2, None])
        )

        # if there are still more than one actions for one moment
        # pick the one with the shortest duration (easiest to regress)
        lens.masked_fill_(inside_gt_seg_mask == 0, float('inf'))
        lens.masked_fill_(inside_regress_range == 0, float('inf'))

        if self.multi_label:
            len_mask = (lens < float('inf')).to(reg_targets.dtype)  # FT x N

            # cls_targets: F T x C; reg_targets F T x C x 2
            gt_label_one_hot = F.one_hot(gt_label, self.num_classes).to(reg_targets.dtype)  # N x C
            cls_targets = len_mask @ gt_label_one_hot
            # to prevent multiple GT actions with the same label and boundaries
            cls_targets.clamp_(min=0.0, max=1.0)
            # FT x N x 2  --> FT x C x 2
            len_mask = len_mask.bool()
            pos_t_idx, pos_gt_idx = torch.where(len_mask > 0)
            pos_cls_idx = gt_label[pos_gt_idx]
            multi_target = torch.zeros((num_pts, self.num_classes, 2), device=reg_targets.device)
            multi_target[pos_t_idx, pos_cls_idx] = reg_targets[pos_t_idx, pos_gt_idx]
            # normalization based on stride
            multi_target /= concat_points[:, 3, None, None]

            return cls_targets, multi_target


        else:
            # F T x N -> F T
            min_len, min_len_inds = lens.min(dim=1)

            # corner case: multiple actions with very similar durations (e.g., THUMOS14)
            min_len_mask = torch.logical_and(
                (lens <= (min_len[:, None] + 1e-3)), (lens < float('inf'))
            ).to(reg_targets.dtype)

            # cls_targets: F T x C; reg_targets F T x 2
            gt_label_one_hot = F.one_hot(
                gt_label, self.num_classes
            ).to(reg_targets.dtype)
            cls_targets = min_len_mask @ gt_label_one_hot

            # to prevent multiple GT actions with the same label and boundaries
            cls_targets.clamp_(min=0.0, max=1.0)
            # OK to use min_len_inds
            reg_targets = reg_targets[range(num_pts), min_len_inds]
            # normalization based on stride
            reg_targets /= concat_points[:, 3, None]

        return cls_targets, reg_targets

    def losses(
            self, fpn_masks,
            out_cls_logits, out_offsets,
            gt_cls_labels, gt_offsets,
            out_start, out_end,
    ):
        # fpn_masks, out_*: F (List) [B, T_i, C]
        # gt_* : B (list) [F T, C]
        # fpn_masks -> (B, FT)
        valid_mask = torch.cat(fpn_masks, dim=1)

        if self.use_trident_head:
            out_start_logits = []
            out_end_logits = []
            for i in range(len(out_start)):
                x = (F.pad(out_start[i], (self.num_bins, 0), mode='constant', value=0)).unsqueeze(-1)  # pad left
                x_size = list(x.size())  # bz, cls_num, T+num_bins, 1
                x_size[-1] = self.num_bins + 1  # bz, cls_num, T+num_bins, num_bins + 1
                x_size[-2] = x_size[-2] - self.num_bins  # bz, cls_num, T+num_bins, num_bins + 1
                x_stride = list(x.stride())
                x_stride[-2] = x_stride[-1]

                x = x.as_strided(size=x_size, stride=x_stride)
                out_start_logits.append(x.permute(0, 2, 1, 3))

                x = (F.pad(out_end[i], (0, self.num_bins), mode='constant', value=0)).unsqueeze(-1)  # pad right
                x = x.as_strided(size=x_size, stride=x_stride)
                out_end_logits.append(x.permute(0, 2, 1, 3))
        else:
            out_start_logits = None
            out_end_logits = None

        # 1. classification loss
        # stack the list -> (B, FT) -> (# Valid, )
        gt_cls = torch.stack(gt_cls_labels)
        if self.multi_label:
            pos_mask = torch.logical_and((gt_cls > 0), valid_mask.unsqueeze(-1))
        else:
            pos_mask = torch.logical_and((gt_cls.sum(-1) > 0), valid_mask)

        decoded_offsets = self.decode_offset(out_offsets, out_start_logits, out_end_logits)  # bz, stack_T, num_class, 2
        decoded_offsets = decoded_offsets[pos_mask]

        if self.use_trident_head:
            # the boundary head predicts the classification score for each categories.
            pred_offsets = decoded_offsets[gt_cls[pos_mask].bool()]
            # cat the predicted offsets -> (B, FT, 2 (xC)) -> # (#Pos, 2 (xC))
            vid = torch.where(gt_cls[pos_mask])[0]
            gt_offsets = torch.stack(gt_offsets)[pos_mask][vid]
        else:
            pred_offsets = decoded_offsets
            gt_offsets = torch.stack(gt_offsets)[pos_mask]

        # update the loss normalizer
        num_pos = pos_mask.sum().item()
        self.loss_normalizer = self.loss_normalizer_momentum * self.loss_normalizer + (
                1 - self.loss_normalizer_momentum
        ) * max(num_pos, 1)

        # gt_cls is already one hot encoded now, simply masking out
        gt_target = gt_cls[valid_mask]

        # optinal label smoothing
        gt_target *= 1 - self.train_label_smoothing
        gt_target += self.train_label_smoothing / (self.num_classes + 1)

        # focal loss
        cls_loss = sigmoid_focal_loss(
            torch.cat(out_cls_logits, dim=1)[valid_mask],
            gt_target,
            reduction='none'
        )

        if self.use_trident_head:
            # couple the classification loss with iou score
            iou_rate = ctr_giou_loss_1d(
                pred_offsets,
                gt_offsets,
                reduction='none'
            )
            rated_mask = gt_target > self.train_label_smoothing / (self.num_classes + 1)
            cls_loss[rated_mask] *= (1 - iou_rate) ** self.iou_weight_power

        cls_loss = cls_loss.sum()
        cls_loss /= self.loss_normalizer

        # 2. regression using IoU/GIoU loss (defined on positive samples)
        if num_pos == 0:
            reg_loss = 0 * pred_offsets.sum()
        else:
            # giou loss defined on positive samples
            reg_loss = ctr_diou_loss_1d(
                pred_offsets,
                gt_offsets,
                reduction='sum'
            )
            reg_loss /= self.loss_normalizer

        if self.train_loss_weight > 0:
            loss_weight = self.train_loss_weight
        else:
            loss_weight = cls_loss.detach() / max(reg_loss.item(), 0.01)

        # return a dict of losses
        final_loss = cls_loss + reg_loss * loss_weight
        return {'cls_loss': cls_loss,
                'reg_loss': reg_loss,
                'final_loss': final_loss}

    @torch.no_grad()
    def inference(
            self,
            video_list,
            points, fpn_masks,
            out_cls_logits, out_offsets,
            out_lb_logits, out_rb_logits,
    ):
        # video_list B (list) [dict]
        # points F (list) [T_i, 4]
        # fpn_masks, out_*: F (List) [B, T_i, C]
        results = []

        # 1: gather video meta information
        vid_idxs = [x['video_id'] for x in video_list]
        vid_fps = [x['fps'] for x in video_list]
        vid_lens = [x['duration'] for x in video_list]
        vid_ft_stride = [x['feat_stride'] for x in video_list]
        vid_ft_nframes = [x['feat_num_frames'] for x in video_list]

        # 2: inference on each single video and gather the results
        # upto this point, all results use timestamps defined on feature grids
        for idx, (vidx, fps, vlen, stride, nframes) in enumerate(
                zip(vid_idxs, vid_fps, vid_lens, vid_ft_stride, vid_ft_nframes)
        ):
            # gather per-video outputs
            cls_logits_per_vid = [x[idx] for x in out_cls_logits]
            offsets_per_vid = [x[idx] for x in out_offsets]
            fpn_masks_per_vid = [x[idx] for x in fpn_masks]

            if self.use_trident_head:
                lb_logits_per_vid = [x[idx] for x in out_lb_logits]
                rb_logits_per_vid = [x[idx] for x in out_rb_logits]
            else:
                lb_logits_per_vid = [None for x in range(len(out_cls_logits))]
                rb_logits_per_vid = [None for x in range(len(out_cls_logits))]

            # inference on a single video (should always be the case)
            results_per_vid = self.inference_single_video(
                points, fpn_masks_per_vid,
                cls_logits_per_vid, offsets_per_vid,
                lb_logits_per_vid, rb_logits_per_vid
            )
            # pass through video meta info
            results_per_vid['video_id'] = vidx
            results_per_vid['fps'] = fps
            results_per_vid['duration'] = vlen
            results_per_vid['feat_stride'] = stride
            results_per_vid['feat_num_frames'] = nframes
            results.append(results_per_vid)

        # step 3: postprocssing
        results = self.postprocessing(results)

        return results

    @torch.no_grad()
    def inference_single_video(
            self,
            points,
            fpn_masks,
            out_cls_logits,
            out_offsets,
            lb_logits_per_vid, rb_logits_per_vid
    ):
        # points F (list) [T_i, 4]
        # fpn_masks, out_*: F (List) [T_i, C]
        segs_all = []
        scores_all = []
        cls_idxs_all = []

        # loop over fpn levels
        for cls_i, offsets_i, pts_i, mask_i, lb_cls_i, rb_cls_i in zip(
                out_cls_logits, out_offsets, points, fpn_masks, lb_logits_per_vid, rb_logits_per_vid
        ):
            pred_prob = (cls_i.sigmoid() * mask_i.unsqueeze(-1)).flatten()

            # Apply filtering to make NMS faster following detectron2
            # 1. Keep seg with confidence score > a threshold
            keep_idxs1 = (pred_prob > self.test_pre_nms_thresh)
            pred_prob = pred_prob[keep_idxs1]
            topk_idxs = keep_idxs1.nonzero(as_tuple=True)[0]

            # 2. Keep top k top scoring boxes only
            num_topk = min(self.test_pre_nms_topk, topk_idxs.size(0))
            pred_prob, idxs = pred_prob.sort(descending=True)
            pred_prob = pred_prob[:num_topk].clone()
            topk_idxs = topk_idxs[idxs[:num_topk]].clone()

            # fix a warning in pytorch 1.9
            pt_idxs = torch.div(
                topk_idxs, self.num_classes, rounding_mode='floor'
            )
            cls_idxs = torch.fmod(topk_idxs, self.num_classes)

            if self.use_trident_head:
                # pad the boarder
                x = (F.pad(lb_cls_i, (self.num_bins, 0), mode='constant', value=0)).unsqueeze(-1)  # pad left
                x_size = list(x.size())  # cls_num, T+num_bins, 1
                x_size[-1] = self.num_bins + 1
                x_size[-2] = x_size[-2] - self.num_bins  # cls_num, T, num_bins + 1
                x_stride = list(x.stride())
                x_stride[-2] = x_stride[-1]

                left = x.as_strided(size=x_size, stride=x_stride)

                x = (F.pad(rb_cls_i, (0, self.num_bins), mode='constant', value=0)).unsqueeze(-1)  # pad right
                right = x.as_strided(size=x_size, stride=x_stride)  # T, cls_num, num_bins
                if self.multi_label:
                    left = left.transpose(0, 1)  # T, cls_num, num_bins
                    right = right.transpose(0, 1)  # T, cls_num, num_bins
            else:
                left = None
                right = None

            decoded_offsets = self.decode_offset(offsets_i, left, right)

            if self.multi_label:
                if self.use_trident_head:
                    offsets = decoded_offsets[pt_idxs, cls_idxs]
                else:
                    offsets = decoded_offsets[pt_idxs, cls_idxs]
            else:
                if self.use_trident_head:
                    offsets = decoded_offsets[cls_idxs, pt_idxs]
                else:
                    offsets = decoded_offsets[pt_idxs]
            pts = pts_i[pt_idxs]

            # 4. compute predicted segments (denorm by stride for output offsets)
            seg_left = pts[:, 0] - offsets[:, 0] * pts[:, 3]
            seg_right = pts[:, 0] + offsets[:, 1] * pts[:, 3]

            pred_segs = torch.stack((seg_left, seg_right), -1)

            # 5. Keep seg with duration > a threshold (relative to feature grids)
            seg_areas = seg_right - seg_left
            keep_idxs2 = seg_areas > self.test_duration_thresh

            # *_all : N (filtered # of segments) x 2 / 1
            segs_all.append(pred_segs[keep_idxs2])
            scores_all.append(pred_prob[keep_idxs2])
            cls_idxs_all.append(cls_idxs[keep_idxs2])

        # cat along the FPN levels (F N_i, C)
        segs_all, scores_all, cls_idxs_all = [
            torch.cat(x) for x in [segs_all, scores_all, cls_idxs_all]
        ]
        results = {'segments': segs_all,
                   'scores': scores_all,
                   'labels': cls_idxs_all}

        return results

    @torch.no_grad()
    def postprocessing(self, results):
        # input : list of dictionary items
        # (1) push to CPU; (2) NMS; (3) convert to actual time stamps
        processed_results = []
        for results_per_vid in results:
            # unpack the meta info
            vidx = results_per_vid['video_id']
            fps = results_per_vid['fps']
            vlen = results_per_vid['duration']
            stride = results_per_vid['feat_stride']
            nframes = results_per_vid['feat_num_frames']
            # 1: unpack the results and move to CPU
            segs = results_per_vid['segments'].detach().cpu()
            scores = results_per_vid['scores'].detach().cpu()
            labels = results_per_vid['labels'].detach().cpu()
            if self.test_nms_method != 'none':
                # 2: batched nms (only implemented on CPU)
                segs, scores, labels = batched_nms(
                    segs, scores, labels,
                    self.test_iou_threshold,
                    self.test_min_score,
                    self.test_max_seg_num,
                    use_soft_nms=(self.test_nms_method == 'soft'),
                    multiclass=self.test_multiclass_nms,
                    sigma=self.test_nms_sigma,
                    voting_thresh=self.test_voting_thresh
                )
            # 3: convert from feature grids to seconds
            if segs.shape[0] > 0:
                segs = (segs * stride + 0.5 * nframes) / fps
                # truncate all boundaries within [0, duration]
                segs[segs <= 0.0] *= 0.0
                segs[segs >= vlen] = segs[segs >= vlen] * 0.0 + vlen
            # 4: repack the results
            processed_results.append(
                {'video_id': vidx,
                 'segments': segs,
                 'scores': scores,
                 'labels': labels}
            )

        return processed_results
