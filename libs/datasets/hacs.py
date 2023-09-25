import os
import json
import h5py
import numpy as np

import torch
from torch.utils.data import Dataset
from torch.nn import functional as F

from .datasets import register_dataset
from .data_utils import truncate_feats
from ..utils import remove_duplicate_annotations


@register_dataset("hacs")
class HacsDataset(Dataset):
    def __init__(
            self,
            is_training,  # if in training mode
            split,  # split, a tuple/list allowing concat of subsets
            feat_folder,  # folder for features
            json_file,  # json file for annotations
            feat_stride,  # temporal stride of the feats
            num_frames,  # number of frames for each feat
            default_fps,  # default fps
            downsample_rate,  # downsample rate for feats
            max_seq_len,  # maximum sequence length during training
            trunc_thresh,  # threshold for truncate an action segment
            crop_ratio,  # a tuple (e.g., (0.9, 1.0)) for random cropping
            input_dim,  # input feat dim
            num_classes,  # number of action categories
            file_prefix,  # feature file prefix if any
            file_ext,  # feature file extension if any
            force_upsampling,  # force to upsample to max_seq_len
            backbone_type,  # feat_type
            additional_feat_folder=None
    ):
        # todo file path, make it general
        if backbone_type == 'slowfast':
            feat_folder = os.path.join(feat_folder, split[0])
        assert os.path.exists(feat_folder) and os.path.exists(json_file)
        assert isinstance(split, tuple) or isinstance(split, list)
        assert crop_ratio == None or len(crop_ratio) == 2
        self.feat_folder = feat_folder
        if file_prefix is not None:
            self.file_prefix = file_prefix
        else:
            self.file_prefix = ''
        self.backbone_type = backbone_type
        self.file_ext = file_ext
        self.json_file = json_file

        # anet uses fixed length features, make sure there is no downsampling
        self.force_upsampling = force_upsampling

        # split / training mode
        self.split = split
        self.is_training = is_training

        # features meta info
        self.feat_stride = feat_stride
        self.num_frames = num_frames
        self.input_dim = input_dim
        self.default_fps = default_fps
        self.downsample_rate = downsample_rate
        self.max_seq_len = max_seq_len
        self.trunc_thresh = trunc_thresh
        self.num_classes = num_classes
        self.label_dict = None
        self.crop_ratio = crop_ratio
        self.use_addtional_feats = additional_feat_folder is not None
        self.additional_feat_folder = additional_feat_folder

        # load database and select the subset
        dict_db, label_dict = self._load_json_db(self.json_file)
        # proposal vs action categories
        assert (num_classes == 1) or (len(label_dict) == num_classes)
        self.data_list = dict_db
        self.label_dict = label_dict

        # dataset specific attributes
        self.db_attributes = {
            'dataset_name': 'HACS',
            'tiou_thresholds': np.linspace(0.5, 0.95, 10),
            # 'tiou_thresholds': np.array([0.5, 0.75, 0.95]),
            'empty_label_ids': []
        }

    def get_attributes(self):
        return self.db_attributes

    def _load_json_db(self, json_file):
        # load database and select the subset
        with open(json_file, 'r') as fid:
            json_data = json.load(fid)
        json_db = json_data['database']

        # if label_dict is not available
        if self.label_dict is None:
            label_dict = {}
            for key, value in json_db.items():
                for act in value['annotations']:
                    label_dict[act['label']] = act['label_id']

        # fill in the db (immutable afterwards)
        dict_db = tuple()
        for key, value in json_db.items():
            # skip the video if not in the split
            if value['subset'].lower() not in self.split:
                continue

            # get fps if available
            if self.default_fps is not None:
                fps = self.default_fps
            elif 'fps' in value:
                fps = value['fps']
            else:
                assert False, "Unknown video FPS."
            duration = float(value['duration'])

            # get annotations if available
            if ('annotations' in value) and (len(value['annotations']) > 0):
                valid_acts = remove_duplicate_annotations(value['annotations'])
                num_acts = len(valid_acts)
                segments = np.zeros([num_acts, 2], dtype=np.float32)
                labels = np.zeros([num_acts, ], dtype=np.int64)
                for idx, act in enumerate(valid_acts):
                    segments[idx][0] = act['segment'][0]
                    segments[idx][1] = act['segment'][1]
                    if self.num_classes == 1:
                        labels[idx] = 0
                    else:
                        labels[idx] = label_dict[act['label']]
            else:
                segments = None
                labels = None
            dict_db += ({'id': key,
                         'fps': fps,
                         'duration': duration,
                         'segments': segments,
                         'labels': labels
                         },)

        return dict_db, label_dict

    def __len__(self):
        return len(self.data_list)

    def __getitem__(self, idx):
        # directly return a (truncated) data point (so it is very fast!)
        # auto batching will be disabled in the subsequent dataloader
        # instead the model will need to decide how to batch / preporcess the data
        video_item = self.data_list[idx]

        # todo load features, make it general
        if self.backbone_type == 'i3d':
            with h5py.File(self.feat_folder, 'r') as h5_fid:
                feats = np.asarray(
                    h5_fid[video_item['id']][()],
                    dtype=np.float32
                )
        elif self.backbone_type == 'slowfast':
            filename = os.path.join(self.feat_folder, video_item['id'] + self.file_ext)
            feats = np.load(filename, allow_pickle=True)
            # 1 x 2304 x T --> T x 2304
            feats = torch.concat([feats['slow_feature'], feats['fast_feature']], dim=1).squeeze(0).transpose(0, 1)
        elif self.backbone_type == 'tsp' or self.backbone_type == 'pose':
            filename = os.path.join(self.feat_folder, self.file_prefix + video_item['id'] + self.file_ext)
            feats = np.load(filename, allow_pickle=True).astype(np.float32)
        elif self.backbone_type == 'videomaev2':
            filename = os.path.join(self.feat_folder, self.file_prefix + video_item['id'] + self.file_ext)
            # feats = torch.load(filename)
            feats = np.load(filename, allow_pickle=True).astype(np.float32)
            # 100 x 1408

        # we support both fixed length features / variable length features
        if self.feat_stride > 0 and (not self.force_upsampling):
            # var length features
            feat_stride, num_frames = self.feat_stride, self.num_frames
            # only apply down sampling here
            if self.downsample_rate > 1:
                feats = feats[::self.downsample_rate, :]
                feat_stride = self.feat_stride * self.downsample_rate
        # case 2: variable length features for input, yet resized for training
        elif self.feat_stride > 0 and self.force_upsampling:
            feat_stride = float(
                (feats.shape[0] - 1) * self.feat_stride + self.num_frames
            ) / self.max_seq_len
            # center the features
            num_frames = feat_stride
        # case 3: fixed length features for input
        else:
            # deal with fixed length feature, recompute feat_stride, num_frames
            seq_len = feats.shape[0]
            assert seq_len <= self.max_seq_len
            if self.force_upsampling:
                # reset to max_seq_len
                seq_len = self.max_seq_len
            feat_stride = video_item['duration'] * video_item['fps'] / seq_len
            # center the features
            num_frames = feat_stride

        # T x C -> C x T
        if isinstance(feats, torch.Tensor):
            feats = feats.transpose(0, 1)
        else:
            feats = torch.from_numpy(np.ascontiguousarray(feats.transpose()))

        # resize the features if needed
        if (feats.shape[-1] != self.max_seq_len) and self.force_upsampling:
            resize_feats = F.interpolate(
                feats.unsqueeze(0),
                size=self.max_seq_len,
                mode='linear',
                align_corners=False
            )
            feats = resize_feats.squeeze(0)

        if self.use_addtional_feats:

            additional_file_name = self.file_prefix + video_item['id'] + '.npy'
            # T, kpt_cls, height, width / T, dim
            additional_feats = np.load(os.path.join(self.additional_feat_folder, additional_file_name),
                                       allow_pickle=True)
            additional_feats = torch.from_numpy(additional_feats).to(torch.float32)

            # todo vector
            additional_feats = additional_feats.flatten(1)  # T, cls, height* width
            additional_feats = additional_feats.transpose(0, 1)  # cls*height* width, T
            # a trick: make the interpolation determinant
            additional_feats = \
                F.interpolate(additional_feats[None], feats.shape[-1], mode='linear', align_corners=True)[0]
        else:
            additional_feats = None

        # convert time stamp (in second) into temporal feature grids
        # ok to have small negative values here
        if video_item['segments'] is not None:
            segments = torch.from_numpy(
                (video_item['segments'] * video_item['fps'] - 0.5 * num_frames) / feat_stride
            )
            labels = torch.from_numpy(video_item['labels'])
            # for activity net, we have a few videos with a bunch of missing frames
            # here is a quick fix for training
            if self.is_training:
                feat_len = feats.shape[1]
                valid_seg_list, valid_label_list = [], []
                for seg, label in zip(segments, labels):
                    if seg[0] >= feat_len:
                        # skip an action outside of the feature map
                        continue
                    # truncate an action boundary
                    valid_seg_list.append(seg.clamp(min=0, max=feat_len))
                    # some weird bug here if not converting to size 1 tensor
                    valid_label_list.append(label.view(1))
                if len(valid_seg_list) == 0:
                    # print(feat_len, segments, video_item)
                    segments, labels = None, None
                else:
                    segments = torch.stack(valid_seg_list, dim=0)
                    labels = torch.cat(valid_label_list)
        else:
            segments, labels = None, None

        # return a data dict
        data_dict = {'video_id': video_item['id'],
                     'feats': feats,  # C x T
                     'segments': segments,  # N x 2
                     'labels': labels,  # N
                     'fps': video_item['fps'],
                     'duration': video_item['duration'],
                     'feat_stride': feat_stride,
                     'feat_num_frames': num_frames,
                     'additional_feats': additional_feats,
                     }

        # no truncation is needed
        # truncate the features during training
        if self.is_training and (segments is not None):
            data_dict = truncate_feats(
                data_dict, self.max_seq_len, self.trunc_thresh, self.crop_ratio
            )

        return data_dict
