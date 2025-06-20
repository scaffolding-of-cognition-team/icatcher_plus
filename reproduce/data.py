import torch
import torch.utils.data as data
from torch.utils.data.distributed import DistributedSampler
from torchvision import transforms
from torchvision.transforms.functional import hflip
import numpy as np
from PIL import Image
import copy
from pathlib import Path
import logging
import csv
import visualize
from augmentations import RandAugment


class DataTransforms:
    def __init__(self, img_size, mean, std):
        self.transformations = {
            'train': transforms.Compose([
                transforms.Resize((img_size, img_size)),
                transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.2, hue=0.05),
                transforms.ToTensor(),
                transforms.Normalize(mean, std)
                # transforms.RandomErasing()
            ]),
            'val': transforms.Compose([
                transforms.Resize((img_size, img_size)),
                transforms.ToTensor(),
                transforms.Normalize(mean, std)
            ]),
            'test': transforms.Compose([
                transforms.Resize((img_size, img_size)),
                transforms.ToTensor(),
                transforms.Normalize(mean, std)
            ])
        }


class LookItDataset(data.Dataset):
    def __init__(self, opt):
        super(LookItDataset, self).__init__()
        self.opt = copy.deepcopy(opt)
        self.paths = self.collect_paths("face_labels_fc")  # change to "face_labels" if face classifier wasn't used
        # self.get_mean_std()
        self.img_processor = DataTransforms(self.opt.image_size,
                                            self.opt.per_channel_mean,
                                            self.opt.per_channel_std).transformations[self.opt.phase]  # ew.
        self.random_augmentor = RandAugment(2, 9)

    def __len__(self):
        return len(self.paths)

    def get_mean_std(self, count=20000):
        """
        calculates mean and std over dataset using count points.
        :param count: the number of iamges to use for calculating statistics
        :return:
        """
        # marchman_bw: mean: [0.48202555, 0.4854607 , 0.48115342], std: [0.1883308 , 0.19372367, 0.18606699]
        permute = np.random.permutation(count)
        psum = np.array([0, 0, 0], dtype=np.float64)
        psum_sq = np.array([0, 0, 0], dtype=np.float64)
        for i in range(count):
            logging.info("{} / {}".format(i, count))
            img_files_seg, box_files_seg, class_seg = self.paths[permute[i]]
            img = Image.open(self.opt.dataset_folder / "faces" / img_files_seg[2]).convert('RGB')
            img = np.array(img.resize((100, 100))) / 255
            psum += np.sum(img, axis=(0, 1))
            psum_sq += np.sum(img ** 2, axis=(0, 1))
        honest_count = 100*100*count
        total_mean = psum / honest_count
        total_std = np.sqrt((psum_sq / honest_count) - (total_mean ** 2))
        return total_mean, total_std

    def check_all_same(self, seg):
        """
        checks if all labels are the same
        :param seg:
        :return:
        """
        for i in range(1, seg.shape[0]):
            if seg[i] != seg[i - 1]:
                return False
        return True

    def collect_paths(self, face_label_name):
        """
        process dataset into tuples of frames
        :param face_label_name: file with face labels
        :return:
        """
        logging.info("{}: Collecting paths for dataloader".format(self.opt.phase))
        if self.opt.phase == "train":
            coding_path = Path(self.opt.dataset_folder, "train", "coding_first")
        else:
            coding_path = Path(self.opt.dataset_folder, "validation", "coding_first")
        coding_names = [f.stem for f in coding_path.glob("*")]
        dataset_folder_path = Path(self.opt.dataset_folder, "faces")
        my_list = []
        logging.info("{}: Collecting paths for dataloader".format(self.opt.phase))
        stats = {}
        stats["video_counter"] = 0
        for name in coding_names:
            stats[name] = {}
            gaze_labels = np.load(str(Path.joinpath(dataset_folder_path, name, f'gaze_labels.npy')))
            gaze_labels_second = None
            if self.opt.use_mutually_agreed:
                second_label_path = Path.joinpath(dataset_folder_path, name, f'gaze_labels_second.npy')
                if second_label_path.exists():
                    gaze_labels_second = np.load(str(second_label_path))
            face_labels = np.load(str(Path.joinpath(dataset_folder_path, name, f'{face_label_name}.npy')))
            stats[name]["datapoints_counter"] = 0
            stats[name]["window_not_fully_coded_counter"] = 0
            stats[name]["coders_disagree_counter"] = 0
            stats[name]["availble_datapoints"] = len(gaze_labels)
            stats[name]["fc_fail_counter"] = 0
            stats[name]["other_fail_counter"] = 0  # face detector failed or no annotation (must change preprocess.py to decouple them)
            logging.info("Video: {}".format(name))
            for frame_number in range(gaze_labels.shape[0]):
                gaze_label_seg = gaze_labels[frame_number:frame_number + self.opt.sliding_window_size]
                face_label_seg = face_labels[frame_number:frame_number + self.opt.sliding_window_size]
                if len(gaze_label_seg) != self.opt.sliding_window_size:
                    stats[name]["window_not_fully_coded_counter"] += 1
                    continue
                if any(face_label_seg < 0):  # a tidy bit too strict?...we can basically afford 1 or two missing labels
                    cur_face = face_label_seg[self.opt.sliding_window_size // 2]
                    if cur_face >= 0:
                        stats[name]["window_not_fully_coded_counter"] += 1
                    else:
                        if cur_face == -1:
                            stats[name]["fc_fail_counter"] += 1
                        elif cur_face == -2:
                            stats[name]["other_fail_counter"] += 1
                        else:
                            raise ValueError
                    continue
                if not self.opt.eliminate_transitions or self.check_all_same(gaze_label_seg):
                    class_seg = gaze_label_seg[self.opt.sliding_window_size // 2]
                    if gaze_labels_second is not None:
                        gaze_label_second = gaze_labels_second[frame_number + self.opt.sliding_window_size // 2]
                        if class_seg != gaze_label_second:
                            stats[name]["coders_disagree_counter"] += 1
                            continue
                    img_files_seg = []
                    box_files_seg = []
                    for i in range(self.opt.sliding_window_size):
                        img_files_seg.append(f'{name}/img/{frame_number + i:05d}_{face_label_seg[i]:01d}.png')
                        box_files_seg.append(f'{name}/box/{frame_number + i:05d}_{face_label_seg[i]:01d}.npy')
                    img_files_seg = img_files_seg[::self.opt.window_stride]
                    box_files_seg = box_files_seg[::self.opt.window_stride]
                    my_list.append((img_files_seg, box_files_seg, class_seg))
                    stats[name]["datapoints_counter"] += 1
            logging.info("extracted {} datapoints from {} frames.".format(stats[name]["datapoints_counter"],
                                                                          stats[name]["availble_datapoints"]))
            logging.info("window_fail: {}, coders_disagree: {}, fc_fail: {}, other_fail: {}\n".format(stats[name]["window_not_fully_coded_counter"],
                                                                                                      stats[name]["coders_disagree_counter"],
                                                                                                      stats[name]["fc_fail_counter"],
                                                                                                      stats[name]["other_fail_counter"]))
            if not my_list:
                logging.info("The video {} has no annotations".format(name))
                continue
            stats["video_counter"] += 1
        total_ava_datapoints = np.sum([stats[key]["availble_datapoints"] for key in stats.keys() if key != "video_counter"])
        total_used_datapoints = np.sum([stats[key]["datapoints_counter"] for key in stats.keys() if key != "video_counter"])
        total_window_fail = np.sum([stats[key]["window_not_fully_coded_counter"] for key in stats.keys() if key != "video_counter"])
        total_coders_disagree = np.sum([stats[key]["coders_disagree_counter"] for key in stats.keys() if key != "video_counter"])
        total_fc_fail = np.sum([stats[key]["fc_fail_counter"] for key in stats.keys() if key != "video_counter"])
        total_other_fail = np.sum([stats[key]["other_fail_counter"] for key in stats.keys() if key != "video_counter"])
        logging.info("dataset video used: {}".format(stats["video_counter"]))
        logging.info("dataset usage percent: {:.2f} %".format(100 * total_used_datapoints / total_ava_datapoints))  
        logging.info("window fails: {:.2f} %".format(100 * total_window_fail / total_ava_datapoints))
        logging.info("coders disagree: {:.2f} %".format(100 * total_coders_disagree / total_ava_datapoints))
        logging.info("fc fails: {:.2f} %".format(100 * total_fc_fail / total_ava_datapoints))
        logging.info("other fails: {:.2f} %".format(100 * total_other_fail / total_ava_datapoints))
        return my_list

    def __getitem__(self, index):
        img_files_seg, box_files_seg, class_seg = self.paths[index]
        flip = 0
        if self.opt.horiz_flip:
            if self.opt.phase == "train":  # also do horizontal flip (but also swap label if necessary for left & right)
                flip = np.random.randint(2)
        imgs = []
        for img_file in img_files_seg:
            img = Image.open(self.opt.dataset_folder / "faces" / img_file).convert('RGB')
            if self.opt.rand_augment:
                if self.opt.phase == "train":  # compose random augmentations with post_processor
                    img = self.random_augmentor(img)
            img = self.img_processor(img)
            if flip:
                img = hflip(img)
            imgs.append(img)
        imgs = torch.stack(imgs)

        boxs = []
        for box_file in box_files_seg:
            box = np.load(self.opt.dataset_folder / "faces" / box_file, allow_pickle=True).item()
            box = torch.tensor([box['face_size'], box['face_ver'], box['face_hor'], box['face_height'], box['face_width']])
            if flip:
                box[2] = 1 - box[2]  # flip horizontal box
            boxs.append(box)
        boxs = torch.stack(boxs)
        boxs = boxs.float()
        imgs = imgs.to(self.opt.device)
        boxs = boxs.to(self.opt.device)
        class_seg = torch.as_tensor(class_seg).to(self.opt.device)
        if flip:
            if class_seg == 1:
                class_seg += 1
            elif class_seg == 2:
                class_seg -= 1
        return {
            'imgs': imgs,  # n x 3 x 100 x 100
            'boxs': boxs,  # n x 5
            'label': class_seg,  # n x 1
            'path': img_files_seg[2]  # n x 1
        }


class MyDataLoader:
    def __init__(self, opt):
        self.opt = copy.deepcopy(opt)
        shuffle = (self.opt.phase == "train")
        self.dataset = LookItDataset(self.opt)
        if self.opt.distributed:
            self.sampler = DistributedSampler(self.dataset,
                                              num_replicas=self.opt.world_size,
                                              rank=self.opt.rank,
                                              shuffle=shuffle,
                                              seed=self.opt.seed)
            self.dataloader = torch.utils.data.DataLoader(self.dataset,
                                                          batch_size=self.opt.batch_size,
                                                          sampler=self.sampler,
                                                          num_workers=0)
        else:
            self.dataloader = torch.utils.data.DataLoader(
                self.dataset,
                batch_size=self.opt.batch_size,
                shuffle=shuffle,
                num_workers=0
            )
        if self.opt.rank == 0:
            self.plot_sample_collage()

    def plot_sample_collage(self, collage_size=25):
        """
        plots a collage of images from dataset
        :param collage_size the size of the collage, must have integer square root
        :return:
        """

        classes = self.opt.gaze_classes  # dict of classes
        bins = [[] for _ in range(len(classes.keys()))]  # bin of images per class
        selected_paths = [[] for _ in range(len(classes.keys()))]  # bin of selected image path per class
        assert np.sqrt(collage_size) == int(np.sqrt(collage_size))  # collage size must have an integer square root
        random_dataloader = torch.utils.data.DataLoader(
            self.dataset,
            batch_size=self.opt.batch_size,
            shuffle=True,
            num_workers=0
        )  # use a random dataloader, with shuffling on so collage is of different children
        iterator = iter(random_dataloader)
        condition = 0
        for bin_counter in range(len(bins)):
            if len(bins[bin_counter]) < collage_size:
                condition = 1
                break
        
        while condition:
            batch_data = next(iterator)
            for i in range(len(batch_data["label"])):
                if len(bins[batch_data["label"][i]]) < collage_size:
                    bins[batch_data["label"][i]].append(batch_data["imgs"][i, 2, ...].permute(1, 2, 0))
                    selected_paths[batch_data["label"][i]].append(batch_data["path"][i])
            
            # Check if we have enough images in each bin
            for bin_counter in range(len(bins)):
                if len(bins[bin_counter]) < collage_size:
                    condition = 1
                    break
        for class_counter, class_id in enumerate(classes.keys()):
            imgs = torch.stack(bins[class_counter]).cpu().numpy()
            imgs = (imgs - np.min(imgs, axis=(1, 2, 3), keepdims=True)) / (np.max(imgs, axis=(1, 2, 3), keepdims=True) - np.min(imgs, axis=(1, 2, 3), keepdims=True))
            save_path = Path(self.opt.experiment_path, "{}_collage_{}.png".format(self.opt.phase, class_id))
            visualize.make_gridview(imgs, ncols=int(np.sqrt(collage_size)), save_path=save_path)
