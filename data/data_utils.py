import random
import csv
from collections import Counter
import re

import numpy as np
import torch

from data.datasets.constant import VALID_CLASS_IDS_200


def per_scene_pad(lang_list, max_len=64):
    """
    @param lang_list: lang json for all sentences, must include scan_id in the json element
    @param max_len: the number for padding, default is 64
    @return: a list of list, with each element in the list containing max_len number of sentences corresponding to
             one scene
    """
    scene_list = {}
    for item in lang_list:
        scan_id = item["scan_id"]
        if scan_id not in scene_list.keys():
            scene_list[scan_id] = [item]
        else:
            scene_list[scan_id].append(item)
    final_list = []
    for key, value in scene_list.items():
        for index in range(0, len(value), max_len):
            if index + max_len < len(value):
                final_list.append(value[index:index + max_len])
            else:
                content = value[index:]
                sampled = random.choices(content, k=max_len)
                final_list.append(sampled)
    return final_list


def convert_pc_to_box(obj_pc):
    xmin = np.min(obj_pc[:,0])
    ymin = np.min(obj_pc[:,1])
    zmin = np.min(obj_pc[:,2])
    xmax = np.max(obj_pc[:,0])
    ymax = np.max(obj_pc[:,1])
    zmax = np.max(obj_pc[:,2])
    center = [(xmin+xmax)/2, (ymin+ymax)/2, (zmin+zmax)/2]
    box_size = [xmax-xmin, ymax-ymin, zmax-zmin]
    return center, box_size


# input txt_ids, txt_masks
def random_word(tokens, tokens_mask, tokenizer, mask_ratio):
    output_label = []
    output_tokens = tokens.clone()
    for i, token in enumerate(tokens):
        if tokens_mask[i] == 0:
            output_label.append(-1)
        else:
            prob = random.random()
            # mask token with 15% probability
            if prob < mask_ratio:
                prob /= mask_ratio

                # 80% randomly change token to mask token
                if prob < 0.8:
                    output_tokens[i] = tokenizer.mask_token_id

                # 10% randomly change token to random token
                elif prob < 0.9:
                    output_tokens[i] = random.choice(list(tokenizer.vocab.items()))[1]

                # -> rest 10% randomly keep current token

                # append current token to output (we will predict these later)
                output_label.append(token.item())
            else:
                # no masking token (will be ignored by loss function later)
                output_label.append(-1)
    output_label = torch.Tensor(output_label).long()
    return output_tokens, output_label


def random_point_cloud(pcd, pcd_mask, mask_ratio):
    assert len(pcd) == len(pcd_mask)
    output_mask = []
    for i in range(len(pcd)):
        if pcd_mask[i] == 0:
            output_mask.append(0)
        else:
            prob = random.random()
            if prob < mask_ratio:
                output_mask.append(0)
            else:
                output_mask.append(1)

    output_mask = torch.tensor(output_mask, dtype=torch.bool)
    return output_mask


class LabelConverter(object):
    def __init__(self, file_path):
        self.raw_name_to_id = {}
        self.nyu40id_to_id = {}
        self.nyu40_name_to_id = {}
        self.scannet_name_to_scannet_id = {'cabinet':0, 'bed':1, 'chair':2, 'sofa':3, 'table':4,
            'door':5, 'window':6,'bookshelf':7,'picture':8, 'counter':9, 'desk':10, 'curtain':11,
            'refrigerator':12, 'shower curtain':13, 'toilet':14, 'sink':15, 'bathtub':16, 'others':17}  
        self.id_to_scannetid = {}
        self.scannet_raw_id_to_raw_name = {}
        self.raw_name_to_scannet_raw_id = {}

        with open(file_path, encoding='utf-8') as fd:
            rd = list(csv.reader(fd, delimiter="\t", quotechar='"'))
            for i in range(1, len(rd)):
                raw_id = i - 1
                scannet_raw_id = int(rd[i][0])
                raw_name = rd[i][1]
                nyu40_id = int(rd[i][4])
                nyu40_name = rd[i][7]
                self.raw_name_to_id[raw_name] = raw_id
                self.scannet_raw_id_to_raw_name[scannet_raw_id] = raw_name
                self.raw_name_to_scannet_raw_id[raw_name] = scannet_raw_id
                self.nyu40id_to_id[nyu40_id] = raw_id
                self.nyu40_name_to_id[nyu40_name] = raw_id
                if nyu40_name not in self.scannet_name_to_scannet_id:
                    self.id_to_scannetid[raw_id] = self.scannet_name_to_scannet_id['others']
                else:
                    self.id_to_scannetid[raw_id] = self.scannet_name_to_scannet_id[nyu40_name]
        
        ### add instance id from org image to pth file
        self.orgInstID_to_id = {id : id - 1 for id in range(1, 257)}
        self.orgInstID_to_id[0] = -100
        
        # add map for scannet 200
        self.scannet_raw_id_to_scannet200_id = {}
        self.scannet200_id_to_scannet_raw_id = {}
        for v, k in enumerate(VALID_CLASS_IDS_200):
            self.scannet_raw_id_to_scannet200_id[k] = v
            self.scannet200_id_to_scannet_raw_id[v] = k

def build_rotate_mat(split, rot_aug=True, rand_angle='axis'):
    if rand_angle == 'random':
        theta = np.random.rand() * np.pi * 2
    else:
        ROTATE_ANGLES = [0, np.pi/2, np.pi, np.pi*3/2]
        theta_idx = np.random.randint(len(ROTATE_ANGLES))
        theta = ROTATE_ANGLES[theta_idx]
    if (theta is not None) and (theta != 0) and (split == 'train') and rot_aug:
        rot_matrix = np.array([
            [np.cos(theta), -np.sin(theta), 0],
            [np.sin(theta), np.cos(theta), 0],
            [0, 0, 1]
        ], dtype=np.float32)
    else:
        rot_matrix = None
    return rot_matrix


def eval_ref_one_sample(pred_bbox, gt_bbox):
    """ Evaluate one reference prediction
    Args:
        pred_bbox: 8 corners of prediction bounding box, (8, 3)
        gt_bbox: 8 corners of ground truth bounding box, (8, 3)
    Returns:
        iou: intersection over union score
    """

    iou = box3d_iou(pred_bbox, gt_bbox)

    return iou

def get_box3d_min_max(corner):
    ''' Compute min and max coordinates for 3D bounding box
        Note: only for axis-aligned bounding boxes
    Input:
        corners: numpy array (8,3), assume up direction is Z (batch of N samples)
    Output:
        box_min_max: an array for min and max coordinates of 3D bounding box IoU
    '''

    min_coord = corner.min(axis=0)
    max_coord = corner.max(axis=0)
    x_min, x_max = min_coord[0], max_coord[0]
    y_min, y_max = min_coord[1], max_coord[1]
    z_min, z_max = min_coord[2], max_coord[2]

    return x_min, x_max, y_min, y_max, z_min, z_max


def box3d_iou(corners1, corners2):
    ''' Compute 3D bounding box IoU.
    Input:
        corners1: numpy array (8,3), assume up direction is Z
        corners2: numpy array (8,3), assume up direction is Z
    Output:
        iou: 3D bounding box IoU
    '''

    x_min_1, x_max_1, y_min_1, y_max_1, z_min_1, z_max_1 = get_box3d_min_max(corners1)
    x_min_2, x_max_2, y_min_2, y_max_2, z_min_2, z_max_2 = get_box3d_min_max(corners2)
    xA = np.maximum(x_min_1, x_min_2)
    yA = np.maximum(y_min_1, y_min_2)
    zA = np.maximum(z_min_1, z_min_2)
    xB = np.minimum(x_max_1, x_max_2)
    yB = np.minimum(y_max_1, y_max_2)
    zB = np.minimum(z_max_1, z_max_2)
    inter_vol = np.maximum((xB - xA), 0) * np.maximum((yB - yA), 0) * np.maximum((zB - zA), 0)
    box_vol_1 = (x_max_1 - x_min_1) * (y_max_1 - y_min_1) * (z_max_1 - z_min_1)
    box_vol_2 = (x_max_2 - x_min_2) * (y_max_2 - y_min_2) * (z_max_2 - z_min_2)
    iou = inter_vol / (box_vol_1 + box_vol_2 - inter_vol + 1e-8)

    return iou


def transform_points(points, transform, translate=True, mode="numpy"):
    """ Apply linear transform to a np array of points.
    Args:
        points (np array [..., 3]): Points to transform.
        transform (np array [3, 4] or [4, 4]): Linear map.
        translate (bool): If false, do not apply translation component of transform.
    Returns:
        transformed points (np array [..., 3])
    """
    # Append ones or zeros to get homogenous coordinates
    if translate:
        if mode == "numpy":
            constant_term = np.ones_like(points[..., :1])
        else:
            constant_term = torch.ones_like(points[..., :1])
    else:
        if mode == "numpy":
            constant_term = np.zeros_like(points[..., :1])
        else:
            constant_term = torch.zeros_like(points[..., :1])
    if mode == "numpy":
        points = np.concatenate((points, constant_term), axis=-1)
        points = np.einsum('nm,...m->...n', transform, points)
    else:
        points = torch.cat((points, constant_term), dim=-1)
        points = torch.einsum('...nm,...m->...n', transform, points)
    return points[..., :3]


def construct_bbox_corners(center, box_size):
    sx, sy, sz = box_size
    x_corners = [sx/2, sx/2, -sx/2, -sx/2, sx/2, sx/2, -sx/2, -sx/2]
    y_corners = [sy/2, -sy/2, -sy/2, sy/2, sy/2, -sy/2, -sy/2, sy/2]
    z_corners = [sz/2, sz/2, sz/2, sz/2, -sz/2, -sz/2, -sz/2, -sz/2]
    corners_3d = np.vstack([x_corners, y_corners, z_corners])
    corners_3d[0,:] = corners_3d[0,:] + center[0]
    corners_3d[1,:] = corners_3d[1,:] + center[1]
    corners_3d[2,:] = corners_3d[2,:] + center[2]
    corners_3d = np.transpose(corners_3d)

    return corners_3d


def is_explicitly_view_dependent(tokens):
    """
    :return: a boolean mask
    """
    target_words = {'front', 'behind', 'back', 'right', 'left', 'facing', 'leftmost', 'rightmost',
                    'looking', 'across'}
    for token in tokens:
        if token in target_words:
            return True
    return False


class ScanQAAnswer(object):
    def __init__(self, answers=None, unk_token='<unk>', ignore_idx=-100):
        if answers is None:
            answers = []
        self.unk_token = unk_token
        self.ignore_idx = ignore_idx
        self.vocab = {x: i for i, x in enumerate(answers)}
        self.rev_vocab = dict((v, k) for k, v in self.vocab.items())

    def itos(self, i):
        if i == self.ignore_idx:
            return self.unk_token
        return self.rev_vocab[i]

    def stoi(self, v):
        if v not in self.vocab:
            return self.ignore_idx
        return self.vocab[v]

    def __len__(self):
        return len(self.vocab)


class SQA3DAnswer(object):
    def __init__(self, answers=None, unk_token='u'):
        if answers is None:
            answers = []
        self.vocab = {x: i for i, x in enumerate(answers)}
        self.rev_vocab = dict((v, k) for k, v in self.vocab.items())
        self.unk_token = unk_token
        self.ignore_idx = self.vocab['u']

    def itos(self, i):
        if i == self.ignore_idx:
            return self.unk_token
        return self.rev_vocab[i]

    def stoi(self, v):
        if v not in self.vocab:
            return self.ignore_idx
        return self.vocab[v]

    def __len__(self):
        return len(self.vocab)


def load_matrix_from_txt(path, shape=(4, 4)):
    with open(path) as f:
        txt = f.readlines()
    txt = ''.join(txt).replace('\n', ' ')
    matrix = [float(v) for v in txt.split()]
    return np.array(matrix).reshape(shape)

def pad_tensors(tensors, lens=None, pad=0):
    assert tensors.shape[0] <= lens
    if tensors.shape[0] == lens:
        return tensors
    shape = list(tensors.shape)
    shape[0] = lens - shape[0]
    res = torch.ones(shape, dtype=tensors.dtype) * pad
    res = torch.cat((tensors, res), dim=0)
    return res

def make_bce_label(indices, num_classes):
    output = torch.zeros(num_classes)
    output[indices] = 1
    return output

def pad_sequence(sequence_list, max_len=None, pad=0, return_mask=False):
    lens = [x.shape[0] for x in sequence_list]
    if max_len is None:
        max_len = max(lens)
        
    shape = list(sequence_list[0].shape)
    shape[0] = max_len
    shape = [len(sequence_list)] + shape
    dtype = sequence_list[0].dtype
    device = sequence_list[0].device
    padded_sequence = torch.ones(shape, dtype=dtype, device=device) * pad
    for i, tensor in enumerate(sequence_list):
        padded_sequence[i, :tensor.shape[0]] = tensor
    padded_sequence = padded_sequence.to(dtype)

    if return_mask:
        mask = torch.arange(max_len).to(device)[None, :] >= torch.LongTensor(lens).to(device)[:, None] # True as masked.
        return padded_sequence, mask
    else:
        return padded_sequence

def pad_sequence_2d(sequence_list, max_height=None, max_width=None, pad=0, return_mask=False):
    if max_height is None:
        max_height = max([x.shape[0] for x in sequence_list])
    if max_width is None:
        max_width = max([x.shape[1] for x in sequence_list])
    
    shape = list(sequence_list[0].shape)
    shape[0] = max_height
    shape[1] = max_width
    shape = [len(sequence_list)] + shape
    dtype = sequence_list[0].dtype
    device = sequence_list[0].device
    padded_sequence = torch.ones(shape, dtype=dtype, device=device) * pad
    for i, tensor in enumerate(sequence_list):
        padded_sequence[i, :tensor.shape[0], :tensor.shape[1]] = tensor
    padded_sequence = padded_sequence.to(dtype)

    if return_mask:
        mask = torch.ones(shape[:3], dtype=bool, device=device) # True as masked.
        for i, tensor in enumerate(sequence_list):
            mask[i, :tensor.shape[0], :tensor.shape[1]] = False
        return padded_sequence, mask
    else:
        return padded_sequence

SQA_TYPES = ['what', 'is', 'how', 'can', 'which', 'other']
def get_sqa_question_type(question):
    question = question.lstrip()
    for i, type in enumerate(SQA_TYPES):
        if question[:len(type)].lower() == type:
            return i
        if i == len(SQA_TYPES) - 1:
            return i


class Vocabulary(object):
    def __init__(self, path=None):
        self.vocab = {}
        self.id_to_vocab = {}
        self.id_to_bert = {}

        if path is not None:
            load_dict = torch.load(path)
            self.vocab = load_dict['vocab']
            self.id_to_vocab = load_dict['id_to_vocab']
            self.id_to_bert = load_dict['id_to_bert']

    def add_token(self, token, bert_id):
        if token in self.vocab.keys():
            return
        id = len(self.vocab) 
        self.vocab[token] = id
        self.id_to_vocab[id] = token
        self.id_to_bert[id] = bert_id

    def token_to_id(self, token):
        return self.vocab[token]

    def id_to_token(self, id):
        return self.id_to_vocab[id]

    def id_to_bert_id(self, id):
        return self.id_to_bert[id]

    def save_vocab(self, path):
        save_dict = {'vocab': self.vocab, "id_to_vocab": self.id_to_vocab,
                     "id_to_bert": self.id_to_bert}
        torch.save(save_dict, path)


def random_caption_word(tokens, tokens_mask, tokenizer, vocab, mask_ratio):
    output_label = []
    output_tokens = tokens.clone()
    for i, token in enumerate(tokens): # 101 cls 102 sep use them as SOS and EOS token
        if tokens_mask[i] == 0 or token == 101:
            output_label.append(-1)
        elif token == 102:
            output_tokens[i] = tokenizer.mask_token_id
            output_label.append(vocab.token_to_id('[EOS]'))
        else:
            prob = random.random()
            # mask token with 15% probability
            if prob < mask_ratio:
                output_tokens[i] = tokenizer.mask_token_id
                output_label.append(vocab.token_to_id(tokenizer.decode([tokens[i]])))
            else:
                # no masking token (will be ignored by loss function later)
                output_label.append(-1)
    output_label = torch.Tensor(output_label).long()
    return output_tokens, output_label


