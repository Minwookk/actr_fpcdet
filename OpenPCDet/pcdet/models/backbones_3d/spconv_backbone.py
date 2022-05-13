from functools import partial

import cv2
import torch
import torch.nn as nn
import numpy as np
from .SemanticSeg.pyramid_ffn import PyramidFeat2D

from pcdet.utils import common_utils
from pcdet.models.model_utils.actr import build as build_actr
from ...utils.spconv_utils import replace_feature, spconv

class objDict:
    @staticmethod
    def to_object(obj: object, **data):
        obj.__dict__.update(data)

class ConfigDict:
    def __init__(self, name):
        self.name = name
    def __getitem__(self, item):
        return getattr(self, item)

def post_act_block(in_channels, out_channels, kernel_size, indice_key=None, stride=1, padding=0,
                   conv_type='subm', norm_fn=None):

    if conv_type == 'subm':
        conv = spconv.SubMConv3d(in_channels, out_channels, kernel_size, bias=False, indice_key=indice_key)
    elif conv_type == 'spconv':
        conv = spconv.SparseConv3d(in_channels, out_channels, kernel_size, stride=stride, padding=padding,
                                   bias=False, indice_key=indice_key)
    elif conv_type == 'inverseconv':
        conv = spconv.SparseInverseConv3d(in_channels, out_channels, kernel_size, indice_key=indice_key, bias=False)
    else:
        raise NotImplementedError

    m = spconv.SparseSequential(
        conv,
        norm_fn(out_channels),
        nn.ReLU(),
    )

    return m


class SparseBasicBlock(spconv.SparseModule):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, norm_fn=None, downsample=None, indice_key=None):
        super(SparseBasicBlock, self).__init__()

        assert norm_fn is not None
        bias = norm_fn is not None
        self.conv1 = spconv.SubMConv3d(
            inplanes, planes, kernel_size=3, stride=stride, padding=1, bias=bias, indice_key=indice_key
        )
        self.bn1 = norm_fn(planes)
        self.relu = nn.ReLU()
        self.conv2 = spconv.SubMConv3d(
            planes, planes, kernel_size=3, stride=stride, padding=1, bias=bias, indice_key=indice_key
        )
        self.bn2 = norm_fn(planes)
        self.downsample = downsample
        self.stride = stride

    def forward(self, x):
        identity = x

        out = self.conv1(x)
        out = replace_feature(out, self.bn1(out.features))
        out = replace_feature(out, self.relu(out.features))

        out = self.conv2(out)
        out = replace_feature(out, self.bn2(out.features))

        if self.downsample is not None:
            identity = self.downsample(x)

        out = replace_feature(out, out.features + identity.features)
        out = replace_feature(out, self.relu(out.features))

        return out


class VoxelBackBone8x(nn.Module):
    def __init__(self, model_cfg, input_channels, grid_size, **kwargs):
        super().__init__()
        self.model_cfg = model_cfg
        norm_fn = partial(nn.BatchNorm1d, eps=1e-3, momentum=0.01)

        self.sparse_shape = grid_size[::-1] + [1, 0, 0]

        self.conv_input = spconv.SparseSequential(
            spconv.SubMConv3d(input_channels, 16, 3, padding=1, bias=False, indice_key='subm1'),
            norm_fn(16),
            nn.ReLU(),
        )
        block = post_act_block

        self.conv1 = spconv.SparseSequential(
            block(16, 16, 3, norm_fn=norm_fn, padding=1, indice_key='subm1'),
        )

        self.conv2 = spconv.SparseSequential(
            # [1600, 1408, 41] <- [800, 704, 21]
            block(16, 32, 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv2', conv_type='spconv'),
            block(32, 32, 3, norm_fn=norm_fn, padding=1, indice_key='subm2'),
            block(32, 32, 3, norm_fn=norm_fn, padding=1, indice_key='subm2'),
        )

        self.conv3 = spconv.SparseSequential(
            # [800, 704, 21] <- [400, 352, 11]
            block(32, 64, 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv3', conv_type='spconv'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm3'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm3'),
        )

        self.conv4 = spconv.SparseSequential(
            # [400, 352, 11] <- [200, 176, 5]
            block(64, 64, 3, norm_fn=norm_fn, stride=2, padding=(0, 1, 1), indice_key='spconv4', conv_type='spconv'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm4'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm4'),
        )

        last_pad = 0
        last_pad = self.model_cfg.get('last_pad', last_pad)
        self.conv_out = spconv.SparseSequential(
            # [200, 150, 5] -> [200, 150, 2]
            spconv.SparseConv3d(64, 128, (3, 1, 1), stride=(2, 1, 1), padding=last_pad,
                                bias=False, indice_key='spconv_down2'),
            norm_fn(128),
            nn.ReLU(),
        )
        self.num_point_features = 128
        self.backbone_channels = {
            'x_conv1': 16,
            'x_conv2': 32,
            'x_conv3': 64,
            'x_conv4': 64
        }



    def forward(self, batch_dict):
        """
        Args:
            batch_dict:
                batch_size: int
                vfe_features: (num_voxels, C)
                voxel_coords: (num_voxels, 4), [batch_idx, z_idx, y_idx, x_idx]
        Returns:
            batch_dict:
                encoded_spconv_tensor: sparse tensor
        """
        voxel_features, voxel_coords = batch_dict['voxel_features'], batch_dict['voxel_coords']
        batch_size = batch_dict['batch_size']
        input_sp_tensor = spconv.SparseConvTensor(
            features=voxel_features,
            indices=voxel_coords.int(),
            spatial_shape=self.sparse_shape,
            batch_size=batch_size
        )

        x = self.conv_input(input_sp_tensor)

        x_conv1 = self.conv1(x)
        x_conv2 = self.conv2(x_conv1)
        x_conv3 = self.conv3(x_conv2)
        x_conv4 = self.conv4(x_conv3)

        # for detection head
        # [200, 176, 5] -> [200, 176, 2]
        out = self.conv_out(x_conv4)

        batch_dict.update({
            'encoded_spconv_tensor': out,
            'encoded_spconv_tensor_stride': 8
        })
        batch_dict.update({
            'multi_scale_3d_features': {
                'x_conv1': x_conv1,
                'x_conv2': x_conv2,
                'x_conv3': x_conv3,
                'x_conv4': x_conv4,
            }
        })
        batch_dict.update({
            'multi_scale_3d_strides': {
                'x_conv1': 1,
                'x_conv2': 2,
                'x_conv3': 4,
                'x_conv4': 8,
            }
        })

        return batch_dict


class VoxelResBackBone8x(nn.Module):
    def __init__(self, model_cfg, input_channels, grid_size, **kwargs):
        super().__init__()
        self.model_cfg = model_cfg
        norm_fn = partial(nn.BatchNorm1d, eps=1e-3, momentum=0.01)

        self.sparse_shape = grid_size[::-1] + [1, 0, 0]

        self.conv_input = spconv.SparseSequential(
            spconv.SubMConv3d(input_channels, 16, 3, padding=1, bias=False, indice_key='subm1'),
            norm_fn(16),
            nn.ReLU(),
        )
        block = post_act_block

        self.conv1 = spconv.SparseSequential(
            SparseBasicBlock(16, 16, norm_fn=norm_fn, indice_key='res1'),
            SparseBasicBlock(16, 16, norm_fn=norm_fn, indice_key='res1'),
        )

        self.conv2 = spconv.SparseSequential(
            # [1600, 1408, 41] <- [800, 704, 21]
            block(16, 32, 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv2', conv_type='spconv'),
            SparseBasicBlock(32, 32, norm_fn=norm_fn, indice_key='res2'),
            SparseBasicBlock(32, 32, norm_fn=norm_fn, indice_key='res2'),
        )

        self.conv3 = spconv.SparseSequential(
            # [800, 704, 21] <- [400, 352, 11]
            block(32, 64, 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv3', conv_type='spconv'),
            SparseBasicBlock(64, 64, norm_fn=norm_fn, indice_key='res3'),
            SparseBasicBlock(64, 64, norm_fn=norm_fn, indice_key='res3'),
        )

        self.conv4 = spconv.SparseSequential(
            # [400, 352, 11] <- [200, 176, 5]
            block(64, 128, 3, norm_fn=norm_fn, stride=2, padding=(0, 1, 1), indice_key='spconv4', conv_type='spconv'),
            SparseBasicBlock(128, 128, norm_fn=norm_fn, indice_key='res4'),
            SparseBasicBlock(128, 128, norm_fn=norm_fn, indice_key='res4'),
        )

        last_pad = 0
        last_pad = self.model_cfg.get('last_pad', last_pad)
        self.conv_out = spconv.SparseSequential(
            # [200, 150, 5] -> [200, 150, 2]
            spconv.SparseConv3d(128, 128, (3, 1, 1), stride=(2, 1, 1), padding=last_pad,
                                bias=False, indice_key='spconv_down2'),
            norm_fn(128),
            nn.ReLU(),
        )
        self.num_point_features = 128
        self.backbone_channels = {
            'x_conv1': 16,
            'x_conv2': 32,
            'x_conv3': 64,
            'x_conv4': 128
        }

    def forward(self, batch_dict):
        """
        Args:
            batch_dict:
                batch_size: int
                vfe_features: (num_voxels, C)
                voxel_coords: (num_voxels, 4), [batch_idx, z_idx, y_idx, x_idx]
        Returns:
            batch_dict:
                encoded_spconv_tensor: sparse tensor
        """
        voxel_features, voxel_coords = batch_dict['voxel_features'], batch_dict['voxel_coords']
        batch_size = batch_dict['batch_size']
        input_sp_tensor = spconv.SparseConvTensor(
            features=voxel_features,
            indices=voxel_coords.int(),
            spatial_shape=self.sparse_shape,
            batch_size=batch_size
        )
        x = self.conv_input(input_sp_tensor)

        x_conv1 = self.conv1(x)
        x_conv2 = self.conv2(x_conv1)
        x_conv3 = self.conv3(x_conv2)
        x_conv4 = self.conv4(x_conv3)

        # for detection head
        # [200, 176, 5] -> [200, 176, 2]
        out = self.conv_out(x_conv4)

        batch_dict.update({
            'encoded_spconv_tensor': out,
            'encoded_spconv_tensor_stride': 8
        })
        batch_dict.update({
            'multi_scale_3d_features': {
                'x_conv1': x_conv1,
                'x_conv2': x_conv2,
                'x_conv3': x_conv3,
                'x_conv4': x_conv4,
            }
        })

        return batch_dict

class VoxelBackBone8xFusion(nn.Module):
    # modified from VoxelBackbone8x + FocalSparseConv
    def __init__(self, model_cfg, input_channels, grid_size, **kwargs):
        super().__init__()
        self.model_cfg = model_cfg
        norm_fn = partial(nn.BatchNorm1d, eps=1e-3, momentum=0.01)

        self.sparse_shape = grid_size[::-1] + [1, 0, 0]

        self.conv_input = spconv.SparseSequential(
            spconv.SubMConv3d(input_channels, 16, 3, padding=1, bias=False, indice_key='subm1'),
            norm_fn(16),
            nn.ReLU(),
        )
        block = post_act_block

        # add
        img_pretrain = model_cfg.get('IMG_PRETRAIN', "checkpoints/deeplabv3_resnet50_coco-cd0a2569.pth")
        self.fusion_pos = model_cfg.get('FUSION_POS', 1)
        self.fusion_method = model_cfg.get('FUSION_METHOD', 'MVX')
        self.voxel_size = torch.Tensor([0.1, 0.05, 0.05]).cuda()
        self.point_cloud_range = torch.Tensor([-3, -40, 0, 1, 40, 70.4]).cuda()
        self.inv_idx =  torch.Tensor([2, 1, 0]).long().cuda()
        self.img_out_channel = 16 if self.fusion_pos == 1 else 64
        model_cfg_seg=dict(
            name='SemDeepLabV3',
            backbone='ResNet50',
            num_class=21, # pretrained on COCO
            args={"feat_extract_layer": ["layer1", "layer2", "layer3"],
                "pretrained_path": img_pretrain},
            channel_reduce={
                "in_channels": [256, 512, 1024],
                "out_channels": [self.img_out_channel, self.img_out_channel, self.img_out_channel],
                "kernel_size": [1, 1, 1],
                "stride": [1, 1, 1],
                "bias": [False, False, False]
            }
        )
        cfg_dict = ConfigDict('SemDeepLabV3')
        objDict.to_object(cfg_dict, **model_cfg_seg)
        self.semseg = PyramidFeat2D(optimize=True, model_cfg=cfg_dict)
        self.img_channel = 16
        if 'ACTR' in self.fusion_method:
            model_name = self.fusion_method
            actr_cfg = model_cfg.get('ACTR_CFG', None)
            lt_cfg = model_cfg.get('LT_CFG', None)
            assert actr_cfg is not None
            self.actr = build_actr(actr_cfg, model_name=model_name, lt_cfg=lt_cfg)
            self.max_num_nev = actr_cfg.get('max_num_ne_voxel', 26000)
            
        #####

        self.conv1 = spconv.SparseSequential(
            block(16, 16, 3, norm_fn=norm_fn, padding=1, indice_key='subm1'),
        )

        self.conv2 = spconv.SparseSequential(
            # [1600, 1408, 41] <- [800, 704, 21]
            block(16 ,32, 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv2', conv_type='spconv'),
            block(32, 32, 3, norm_fn=norm_fn, padding=1, indice_key='subm2'),
            block(32, 32, 3, norm_fn=norm_fn, padding=1, indice_key='subm2'),
        )

        self.conv3 = spconv.SparseSequential(
            # [800, 704, 21] <- [400, 352, 11]
            block(32, 64, 3, norm_fn=norm_fn, stride=2, padding=1, indice_key='spconv3', conv_type='spconv'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm3'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm3'),
        )

        self.conv4 = spconv.SparseSequential(
            # [400, 352, 11] <- [200, 176, 5]
            block(64, 64, 3, norm_fn=norm_fn, stride=2, padding=(0, 1, 1), indice_key='spconv4', conv_type='spconv'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm4'),
            block(64, 64, 3, norm_fn=norm_fn, padding=1, indice_key='subm4'),
        )

        last_pad = 0
        last_pad = self.model_cfg.get('last_pad', last_pad)
        self.conv_out = spconv.SparseSequential(
            # [200, 150, 5] -> [200, 150, 2]
            spconv.SparseConv3d(64, 128, (3, 1, 1), stride=(2, 1, 1), padding=last_pad,
                                bias=False, indice_key='spconv_down2'),
            norm_fn(128),
            nn.ReLU(),
        )
        self.num_point_features = 128
        self.backbone_channels = {
            'x_conv1': 16,
            'x_conv2': 32,
            'x_conv3': 64,
            'x_conv4': 64
        }



    def point_fusion(self, x, batch_dict, voxel_stride=1):
        def construct_multimodal_features(x, x_rgb, batch_dict, fuse_sum=False):
            """
                Construct the multimodal features with both lidar sparse features and image features.
                Args:
                    x: [N, C] lidar sparse features
                    x_rgb: [b, c, h, w] image features
                    batch_dict: input and output information during forward
                    fuse_sum: bool, manner for fusion, True - sum, False - concat

                Return:
                    image_with_voxelfeatures: [N, C] fused multimodal features
            """
            batch_index = x.indices[:, 0]
            spatial_indices = x.indices[:, 1:] * voxel_stride
            voxels_3d = spatial_indices * self.voxel_size + self.point_cloud_range[:3]
            calibs = batch_dict['calib']
            batch_size = batch_dict['batch_size']
            h, w = batch_dict['images'].shape[2:]

            if self.fusion_method == 'MVX':
                if not x_rgb[0].shape == batch_dict['images'].shape:
                    x_rgb[0]= nn.functional.interpolate(x_rgb[0], (h, w), mode='bilinear')

            image_with_voxelfeatures = []
            voxels_2d_int_list = []
            filter_idx_list = []
            pts_list = []
            coor_2d_list = []
            pts_feats_list = []
            num_points = []
            for b in range(batch_size):
                x_rgb_batch = x_rgb[0][b]

                calib = calibs[b]
                voxels_3d_batch = voxels_3d[batch_index==b]
                voxel_features_sparse = x.features[batch_index==b]
                num_points.append(voxel_features_sparse.shape[0])

                # Reverse the point cloud transformations to the original coords.
                if 'noise_scale' in batch_dict:
                    voxels_3d_batch[:, :3] /= batch_dict['noise_scale'][b]
                if 'noise_rot' in batch_dict:
                    voxels_3d_batch = common_utils.rotate_points_along_z(voxels_3d_batch[:, self.inv_idx].unsqueeze(0), -batch_dict['noise_rot'][b].unsqueeze(0))[0, :, self.inv_idx]
                if 'flip_x' in batch_dict:
                    voxels_3d_batch[:, 1] *= -1 if batch_dict['flip_x'][b] else 1
                if 'flip_y' in batch_dict:
                    voxels_3d_batch[:, 2] *= -1 if batch_dict['flip_y'][b] else 1

                voxels_2d, _ = calib.lidar_to_img(voxels_3d_batch[:, self.inv_idx].cpu().numpy())
                voxels_2d_norm = voxels_2d / np.array([w, h])

                voxels_2d_int = torch.Tensor(voxels_2d).to(x_rgb_batch.device).long()

                filter_idx = (0<=voxels_2d_int[:, 1]) * (voxels_2d_int[:, 1] < h) * (0<=voxels_2d_int[:, 0]) * (voxels_2d_int[:, 0] < w)

                filter_idx_list.append(filter_idx)
                voxels_2d_int = voxels_2d_int[filter_idx]
                voxels_2d_int_list.append(voxels_2d_int)


                if 'ACTR' in self.fusion_method:
                    coor_2d_list.append(voxels_2d_norm)
                    pts_list.append(voxels_3d_batch)
                    pts_feats_list.append(voxel_features_sparse)

                elif self.fusion_method == 'MVX':
                    image_features_batch = torch.zeros((voxel_features_sparse.shape[0], x_rgb_batch.shape[0]), device=x_rgb_batch.device)
                    image_features_batch[filter_idx] = x_rgb_batch[:, voxels_2d_int[:, 1], voxels_2d_int[:, 0]].permute(1, 0)
                    if fuse_sum:
                        image_with_voxelfeature = image_features_batch + voxel_features_sparse
                    else:
                        image_with_voxelfeature = torch.cat([image_features_batch, voxel_features_sparse], dim=1)
                    image_with_voxelfeatures.append(image_with_voxelfeature)

            if 'ACTR' in self.fusion_method:
                n_max = 0
                pts_feats_b = torch.zeros((batch_size, self.max_num_nev, x.features.shape[1])).cuda()
                coor_2d_b = torch.zeros((batch_size, self.max_num_nev, 2)).cuda()
                pts_b = torch.zeros((batch_size, self.max_num_nev, 3)).cuda()
                for b in range(batch_size):
                    if False:
                        img = (batch_dict['images'][b] * 255).to(torch.int).permute((1, 2, 0)).cpu().detach().numpy().astype(np.uint8)[..., [2, 1, 0]]
                        voxels_2d = (coor_2d_list[b] * np.array([w, h])).astype(np.int)

                        for pts in voxels_2d:
                            if pts[0] < 0 or pts[1] < 0 or pts[1] > h or pts[0] > w:
                                continue
                            img = cv2.circle(img.copy(), (pts[0], pts[1]), radius=1, color=(0, 0, 255), thickness=-1)
                        cv2.imwrite('test.png', img)
                        import pdb; pdb.set_trace()
                        abcd = 1

                    pts_b[b, :pts_list[b].shape[0]] = pts_list[b]
                    coor_2d_b[b, :pts_list[b].shape[0]] = torch.tensor(coor_2d_list[b]).cuda()
                    pts_feats_b[b, :pts_list[b].shape[0]] = pts_feats_list[b]
                    n_max = max(n_max, pts_list[b].shape[0])
                enh_feat = self.actr(v_feat=pts_feats_b[:, :n_max], grid=coor_2d_b[:, :n_max],
                                     i_feats=x_rgb, lidar_grid=pts_b[:, :n_max, self.inv_idx])
                enh_feat_cat = torch.cat(
                    [f[:np] for f, np in zip(enh_feat, num_points)])
                if fuse_sum:
                    enh_feat_cat = enh_feat_cat + x.features
                else:
                    enh_feat_cat = torch.cat([enh_feat_cat, x.features], dim=1)
                return enh_feat_cat

            elif self.fusion_method == 'MVX':
                image_with_voxelfeatures = torch.cat(image_with_voxelfeatures)
                return image_with_voxelfeatures

        img_dict = self.semseg(batch_dict['images'])
        x_rgb = []
        for key in img_dict:
            x_rgb.append(img_dict[key])
        features_multimodal = construct_multimodal_features(x, x_rgb, batch_dict, True)
        x_mm = spconv.SparseConvTensor(features_multimodal, x.indices, x.spatial_shape, x.batch_size)
        return x_mm


    def forward(self, batch_dict):
        """
        Args:
            batch_dict:
                batch_size: int
                vfe_features: (num_voxels, C)
                voxel_coords: (num_voxels, 4), [batch_idx, z_idx, y_idx, x_idx]
        Returns:
            batch_dict:
                encoded_spconv_tensor: sparse tensor
        """
        voxel_features, voxel_coords = batch_dict['voxel_features'], batch_dict['voxel_coords']
        batch_size = batch_dict['batch_size']
        input_sp_tensor = spconv.SparseConvTensor(
            features=voxel_features,
            indices=voxel_coords.int(),
            spatial_shape=self.sparse_shape,
            batch_size=batch_size
        )

        x = self.conv_input(input_sp_tensor)

        x_conv1 = self.conv1(x)
        if self.fusion_pos == 1:
            x_conv1 = self.point_fusion(x_conv1, batch_dict, voxel_stride=1)

        x_conv2 = self.conv2(x_conv1)
        x_conv3 = self.conv3(x_conv2)
        x_conv4 = self.conv4(x_conv3)
        if self.fusion_pos == 4:
            x_conv4 = self.point_fusion(x_conv4, batch_dict, voxel_stride=8)

        # for detection head
        # [200, 176, 5] -> [200, 176, 2]
        out = self.conv_out(x_conv4)

        batch_dict.update({
            'encoded_spconv_tensor': out,
            'encoded_spconv_tensor_stride': 8
        })
        batch_dict.update({
            'multi_scale_3d_features': {
                'x_conv1': x_conv1,
                'x_conv2': x_conv2,
                'x_conv3': x_conv3,
                'x_conv4': x_conv4,
            }
        })
        batch_dict.update({
            'multi_scale_3d_strides': {
                'x_conv1': 1,
                'x_conv2': 2,
                'x_conv3': 4,
                'x_conv4': 8,
            }
        })

        return batch_dict
