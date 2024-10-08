import time
import torch
from torch import nn
import numpy as np
from abc import ABC
from scipy.ndimage import gaussian_filter
from typing import Tuple, List, Literal, Generator
from detectron2.projects.segmentation.data import ImageSample

class BaseSplitCombiner(ABC):
    def __init__(
            self,
            margin: Tuple[int, int],
            crop_size: Tuple[int, int],
            pad_value: float,
            pad_mode: Literal['constant', 'reflect', 'replicate', 'circular'] = 'constant',
            combine_method: Literal['max', 'avr'] = 'max',
            save_features: bool = False,
            device: Literal['cpu', 'cuda'] = 'cpu'):

        self.margin = np.asarray(margin)
        self.crop_size = np.asarray(crop_size)
        self.pad_value = pad_value
        self.pad_mode = pad_mode
        self.combine_method = combine_method
        self.save_features = save_features
        self.device = device

        self.stride=self.crop_size - 2*self.margin
        assert (self.stride>0).all(), f'Margin {margin} should be less than half of crop_size {crop_size}.'
        self.data_sample = None
        self.output_seg = None
        self.cur_patch_idx = 0

    def split(self, data_sample: ImageSample) -> Generator:
        self.data_sample = data_sample

        image = data_sample.image
        label = data_sample.label
        if self.device != 'cpu':
            image = image.to(self.device)
            label = label.to(self.device)

        _,h,w = image.shape
        [nh, nw] = [int(x) for x in (np.ceil(np.asarray([h, w])/self.stride))]

        # pytorch pad顺序：(x0, x1, y0, y1)
        pad = (self.margin[1], self.margin[1], self.margin[0], self.margin[0])
        if self.pad_mode == 'constant':
            image = torch.nn.functional.pad(image, pad, self.pad_mode, self.pad_value)
        else:
            image = torch.nn.functional.pad(image, pad, self.pad_mode)
        # 转成(y0, y1, x0, x1)
        self.pad = (pad[2], pad[3], pad[0], pad[1])

        self.coord_list = []
        for i in range(nh):
            for j in range(nw):
                coord = self._get_patch_coord((i, j), (nh, nw), (h, w), self.crop_size, self.stride)
                patch = image[..., coord[0]:coord[1], coord[2]:coord[3]]
                label_patch = label[..., coord[0]:coord[1], coord[2]:coord[3]]
                self.coord_list.append(coord)
                yield type(data_sample)(image=patch, label_patch=label_patch)


    def combine(self, patch_sample: ImageSample):
        img_shape = self.data_sample.img_size
        if self.output_seg is None:
            output_channel = patch_sample.pred.shape[0]
            self.output_seg = torch.full(
                (output_channel,)+img_shape,
                0 if self.combine_method == 'avr' else -1000,
                dtype = patch_sample.pred.dtype,
                device = patch_sample.pred.device)
            if self.combine_method == 'avr':
                self.patch_cnt = torch.zeros(
                    (1, )+img_shape,
                    dtype = torch.uint8,
                    device=patch_sample.pred.device
                )

        coord = self.coord_list[self.cur_patch_idx]
        self.cur_patch_idx +=1

        ori_start = np.array(coord[::2]+self.margin-np.array(self.pad[::2]))
        ori_end = np.array(coord[1::2] - self.margin - np.array(self.pad[1::2]))
        start = np.maximum(ori_start, 0)
        end = np.minimum(ori_end, img_shape)
        offset_start = start - ori_start
        offset_end = ori_end - end

        patch_pred = patch_sample.pred[
            :,
            self.margin[0]+offset_start[0]: self.margin[0]+self.stride[0]-offset_end[0],
            self.margin[1] + offset_start[1]: self.margin[1] + self.stride[1] - offset_end[1]
        ]

        if self.combine_method == 'avr':
            self.output_seg[:, start[0]:end[0], start[1]:end[1]]+=patch_pred
            self.patch_cnt[:, start[0]:end[0], start[1]:end[1]] +=1
        else:
            self.output_seg[:, start[0]:end[0], start[1]:end[1]] \
                = torch.maximum(patch_pred, self.output_seg[:, start[0]:end[0], start[1]:end[1]])

    def return_output(self) -> ImageSample:
        pred = self.output_seg

        if self.combine_method == 'avr':
            pred /= self.patch_cnt.clip_(min=1)

        output_sample = self.data_sample
        output_sample.pred = pred

        self.output_seg = None
        self.patch_cnt = None
        self.data_sample = None
        self.cur_patch_idx = 0
        return output_sample


    def _get_patch_coord(self,
                         patch_idx: Tuple[int, int],
                         patch_num: Tuple[int, int],
                         shape: Tuple[int, int],
                         crop_size: Tuple[int, int],
                         stride: Tuple[int, int],
                         ):
        coord = []
        for i in range(len(patch_idx)):
            if patch_idx[i] == patch_num[i]-1 and patch_idx[i]>0:
                end = shape[i]
                start = end - crop_size[i]
            else:
                start = patch_idx[i]*stride[i]
                end = start + crop_size[i]
            coord+=[start, end]
        return coord


class SplitCombiner(BaseSplitCombiner):
    def __init__(
            self,
            crop_size: Tuple[int, int],
            stride: Tuple[int, int],
            combine_method: Literal['max', 'avr', 'gw'] = 'max',
            save_features: bool = False,
            save_cams: bool = False,
            device: Literal['cpu', 'cuda'] = 'cpu',
            sigma: float = 0.25):

        self.crop_size = np.asarray(crop_size)
        self.stride = np.asarray(stride)
        self.combine_method = combine_method
        self.save_features = save_features
        self.save_cams = save_cams
        self.device = device
        self.sigma = sigma

        self.reset()

    def reset(self):
        self.data_sample = None
        self.output_seg = None
        self.patch_cnt = None

        if self.save_cams:
            self.cam = None
            self.patch_cnt_cam = None

        if self.save_features:
            self.x = None
            self.y = None
            self.logits = None
            self.patch_cnt_x = None
            self.patch_cnt_y = None
            self.patch_cnt_logit = None

        self.cur_patch_idx = 0

    def split(self, data_sample: ImageSample) -> Generator:
        self.data_sample = data_sample

        image = data_sample.image
        label = data_sample.label
        if self.device != 'cpu':
            image = image.to(self.device)
            label = label.to(self.device)
        _,h,w = image.shape
        [nh, nw] = [int(x) for x in (np.ceil((np.asarray([h, w])-self.crop_size)/self.stride + 1))]

        self.coord_list = []
        for i in range(nh):
            for j in range(nw):
                coord = self._get_patch_coord((i, j), (nh, nw), (h, w), self.crop_size, self.stride)
                patch = image[..., coord[0]:coord[1], coord[2]:coord[3]]
                label_patch = label[..., coord[0]:coord[1], coord[2]:coord[3]]
                self.coord_list.append(coord)
                yield type(data_sample)(image=patch, label = label_patch)

    def patch_to_output(self, output, patch_cnt, img_shape, patch, start, end):
        if output is None:
            output_channel = patch.shape[0]
            output = torch.full(
                (output_channel,) + img_shape,
                -1000 if self.combine_method == 'max' else 0,
                dtype=patch.dtype,
                device=patch.device)

            if self.combine_method == 'avr':
                patch_cnt = torch.zeros(
                    (1,) + img_shape,
                    dtype=torch.uint8,
                    device=patch.device
                )
            elif self.combine_method == 'gw':
                patch_cnt = torch.zeros(
                    (1,) + img_shape,
                    dtype=torch.float32,
                    device=patch.device
                )

        # resize
        # print(start, end)
        if np.all(patch.shape[-2:] != self.crop_size):
            patch = nn.functional.interpolate(patch[None], size=tuple(self.crop_size), mode='bicubic', align_corners=True)[0]
        # patch to output
        if self.combine_method == 'avr':
            output[:, start[0]:end[0], start[1]:end[1]] += patch
            patch_cnt[:, start[0]:end[0], start[1]:end[1]] += 1
        elif self.combine_method == 'max':
            output[:, start[0]:end[0], start[1]:end[1]] \
                = torch.maximum(patch, output[:, start[0]:end[0], start[1]:end[1]])
        elif self.combine_method == 'gw':
            gaussian_map = torch.from_numpy(self.get_gaussian(self.crop_size, sigma=self.sigma)).to(patch.device)
            output[:, start[0]:end[0], start[1]:end[1]] += patch * gaussian_map
            patch_cnt[:, start[0]:end[0], start[1]:end[1]] += gaussian_map

        return output, patch_cnt


    def combine(self, patch_sample: ImageSample):
        img_shape = self.data_sample.img_size

        coord = self.coord_list[self.cur_patch_idx]
        self.cur_patch_idx += 1

        start = np.array(coord[::2])
        end = np.array(coord[1::2])

        self.output_seg, self.patch_cnt = self.patch_to_output(
            self.output_seg, self.patch_cnt, img_shape, patch_sample.pred, start, end)

        if self.save_cams:
            if self.cam is None:
                nc = len(patch_sample.cam)

            self.cam, self.patch_cnt_cam = \
                zip(*[self.patch_to_output(output_cam, patch_cnt_cam, img_shape, patch_cam, start, end)
                      for (output_cam, patch_cnt_cam, patch_cam) in
                      zip([self.cam]*nc if self.cam is None else self.cam,
                          [self.patch_cnt_cam]*nc if self.patch_cnt_cam is None else self.patch_cnt_cam,
                          patch_sample.cam)])

        if self.save_features:
            if self.x is None:

                nx = len(patch_sample.x)
                ny = len(patch_sample.y)
                nl = len(patch_sample.logits)

            self.x, self.patch_cnt_x = \
                zip(*[self.patch_to_output(output_x, patch_cnt_x, img_shape, patch_x, start, end)
                      for (output_x, patch_cnt_x, patch_x) in
                      zip([self.x] * nx if self.x is None else self.x,
                          [self.patch_cnt_x] * nx if self.patch_cnt_x is None else self.patch_cnt_x,
                          patch_sample.x)])

            self.y, self.patch_cnt_y = \
                zip( *[self.patch_to_output(output_y, patch_cnt_y, img_shape, patch_y, start, end)
                       for (output_y, patch_cnt_y, patch_y) in
                       zip([self.y] * ny if self.y is None else self.y,
                           [self.patch_cnt_y] * ny if self.patch_cnt_y is None else self.patch_cnt_y,
                           patch_sample.y)])

            self.logits, self.patch_cnt_logit = \
                zip(*[self.patch_to_output(output_logit, patch_cnt_logit, img_shape, patch_logit, start, end)
                      for (output_logit, patch_cnt_logit, patch_logit) in
                      zip([self.logits] * nl if self.logits is None else self.logits,
                          [self.patch_cnt_logit] * nl if self.patch_cnt_logit is None else self.patch_cnt_logit,
                          patch_sample.logits)])

    # def combine(self, patch_sample: ImageSample):
    #     img_shape = self.data_sample.img_size
    #     # print(patch_sample.pred.shape)
    #     if self.output_seg is None:
    #         output_channel = patch_sample.pred.shape[0]
    #         self.output_seg = torch.full(
    #             (output_channel,)+img_shape,
    #             -1000 if self.combine_method == 'max' else 0,
    #             dtype = patch_sample.pred.dtype,
    #             device = patch_sample.pred.device)
    #         if self.combine_method == 'avr':
    #             self.patch_cnt = torch.zeros(
    #                 (1, )+img_shape,
    #                 dtype = torch.uint8,
    #                 device=patch_sample.pred.device
    #             )
    #         if self.combine_method == 'gw':
    #             self.patch_gw = torch.zeros(
    #                 (1, )+img_shape,
    #                 dtype = torch.float32,
    #                 device = patch_sample.pred.device
    #             )
    #
    #     coord = self.coord_list[self.cur_patch_idx]
    #     self.cur_patch_idx +=1
    #
    #     start = np.array(coord[::2])
    #     end = np.array(coord[1::2])
    #
    #     patch_pred = patch_sample.pred
    #     # if self.x == None or self.y == None:
    #     self.x = patch_sample.x
    #     self.y = patch_sample.y
    #     self.logits = patch_sample.logits
    #     self.label_patch = patch_sample.label_patch
    #
    #     if self.combine_method == 'avr':
    #         self.output_seg[:, start[0]:end[0], start[1]:end[1]]+=patch_pred
    #         self.patch_cnt[:, start[0]:end[0], start[1]:end[1]] +=1
    #     elif self.combine_method == 'max':
    #         self.output_seg[:, start[0]:end[0], start[1]:end[1]] \
    #             = torch.maximum(patch_pred, self.output_seg[:, start[0]:end[0], start[1]:end[1]])
    #     elif self.combine_method == 'gw':
    #         gaussian_map = torch.from_numpy(self.get_gaussian(self.crop_size, sigma=self.sigma)).to(patch_sample.pred.device)
    #         self.output_seg[:, start[0]:end[0], start[1]:end[1]] += patch_pred*gaussian_map
    #         self.patch_gw[:, start[0]:end[0], start[1]:end[1]] += gaussian_map

    def get_gaussian(self, patch_size, sigma = 0.25):
        tmp = np.zeros(patch_size)
        coord = [x//2 for x in patch_size]
        sigmas = [x*sigma for x in patch_size]
        tmp[tuple(coord)] = 1
        gaussian_map = gaussian_filter(tmp, sigmas, 0, mode='constant', cval=0)
        gaussian_map /= np.max(gaussian_map)
        return gaussian_map

    def norm(self, output, patch_cnt):
        patch_cnt == patch_cnt.clip_(min=1) if self.combine_method == 'avr' else patch_cnt
        if self.combine_method in ['avr', 'gw']:
            output /=patch_cnt
        return output


    def return_output(self) -> ImageSample:

        output_sample = self.data_sample
        output_sample.pred = self.norm(self.output_seg, self.patch_cnt)

        if self.save_cams:
            output_sample.cam = [self.norm(cam, patch_cnt_cam) for (cam, patch_cnt_cam) in zip(self.cam, self.patch_cnt_cam)]

        if self.save_features:
            output_sample.x = [self.norm(x, patch_cnt_x) for (x, patch_cnt_x) in zip(self.x, self.patch_cnt_x)]
            output_sample.y = [self.norm(y, patch_cnt_y) for (y, patch_cnt_y) in zip(self.y, self.patch_cnt_y)]
            output_sample.logits = \
                [self.norm(logit, patch_cnt_logit) for (logit, patch_cnt_logit) in zip(self.logits, self.patch_cnt_logit)]

        self.reset()

        return output_sample



class SplitCombineModelWrapper(torch.nn.Module):
    def __init__(
            self,
            model: torch.nn.Module,
            split_combiner: SplitCombiner,
            batch_size: int = 1
    ):
        super().__init__()
        self.model = model
        self.split_combiner = split_combiner
        self.batch_size = batch_size

    def forward(self, data_sample: List[ImageSample]) -> List[ImageSample]:
        output_samples = []
        for sample in data_sample:
            ts = time.time()

            batch = []
            for idx, patch_sample in enumerate(self.split_combiner.split(sample)):
                batch.append(patch_sample)
                if len(batch) == self.batch_size:
                    batch_output = self.model(batch)
                    for patch_output in batch_output:
                        self.split_combiner.combine(patch_output)
                    batch.clear()

            if len(batch):
                batch_output = self.model(batch)
                for patch_output in batch_output:
                    self.split_combiner.combine(patch_output)

            output_sample = self.split_combiner.return_output()
            output_samples.append(output_sample)

            torch.cuda.synchronize()
            te = time.time()
            print(f'sample.name: {sample.img_name}, patch num: {idx}, inference time: {te-ts}s')

        return output_samples


