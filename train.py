import numpy as np
import os
import plac

import cv2
from imgaug import augmenters as iaa

import imgaug as ia

import tensorflow.keras as keras
import tensorflow.keras.backend as K
from tensorflow.keras.optimizers import SGD
from tensorflow.keras.callbacks import ModelCheckpoint
from tensorflow.keras.utils import Sequence

from model import MobileDetectNetModel

from sgdr import SGDRScheduler


class MobileDetectNetSequence(Sequence):
    def __init__(self,
                 path: str,
                 stage: str = "train",
                 batch_size: int = 24,
                 resize_width: int = 224,
                 resize_height: int = 224,
                 coverage_width: int = 7,
                 coverage_height: int = 7,
                 bboxes_width: int = 7,
                 bboxes_height: int = 7
                 ):

        self.images = []
        self.images_filenames = []
        self.labels = []

        for r, d, f in os.walk(os.path.join(path, "images")):
            for file in f:
                self.images.append(os.path.join(r, file))
                self.labels.append(os.path.join(path, "labels", (file.split(".")[0] + ".txt")))

        self.batch_size = batch_size
        self.resize_width = resize_width
        self.resize_height = resize_height
        self.coverage_width = coverage_width
        self.coverage_height = coverage_height
        self.bboxes_width = bboxes_width
        self.bboxes_height = bboxes_height

        self.seq = MobileDetectNetSequence.create_augmenter(stage)

        self.anchors = []

        for y in range(0, self.coverage_height):
            for x in range(0, self.coverage_width):

                for s_idx, scale in enumerate([1.0, 2.0, 3.0]):
                    for a_idx, aspect in enumerate([1.0, 4 / 3, 3 / 4]):
                        scale_width = scale * aspect
                        scale_height = scale * (1 / aspect)

                        # Box before scaling
                        x1 = x
                        y1 = y
                        x2 = x + 1
                        y2 = y + 1

                        width_initial = x2 - x1
                        height_initial = y2 - y1

                        width_final = width_initial * scale_width
                        height_final = height_initial * scale_height

                        delta_width = width_final - width_initial
                        delta_height = height_final - height_initial

                        x1 -= delta_width / 2
                        x2 += delta_width / 2

                        y1 -= delta_height / 2
                        y2 += delta_height / 2

                        anchor = ia.BoundingBox(x1, y1, x2, y2)

                        self.anchors.append(anchor)

    def __len__(self):
        # TODO: Do stuff with "remainder" training data
        return int(np.floor(len(self.images) / float(self.batch_size)))

    def __getitem__(self, idx):

        input_image = np.zeros((self.batch_size, self.resize_height, self.resize_width, 3))

        output_region = np.zeros((self.batch_size, self.bboxes_height, self.bboxes_width, 9))
        output_bboxes = np.zeros((self.batch_size, self.bboxes_height, self.bboxes_width, 4))
        output_class = np.zeros((self.batch_size, self.bboxes_height, self.bboxes_width, 1))

        for i in range(0, self.batch_size):

            seq_det = self.seq.to_deterministic()

            image = cv2.imread(self.images[idx * self.batch_size + i])
            old_shape = image.shape
            image = cv2.resize(image, (self.resize_height, self.resize_width))

            bboxes = MobileDetectNetSequence.load_kitti_label(image,
                                                              scale=(image.shape[0] / old_shape[0],
                                                                     image.shape[1] / old_shape[1]),
                                                              label=self.labels[idx * self.batch_size + i])

            image_aug = seq_det.augment_image(image)
            bboxes_aug = seq_det.augment_bounding_boxes(bboxes).remove_out_of_image().clip_out_of_image()

            # Work on building a batch
            input_image[i] = (image_aug.astype(np.float32) / 127.5) - 1.  # "tf" style normalization

            for bbox_unscaled in bboxes_aug.bounding_boxes:

                for y in range(0, self.coverage_height):
                    for x in range(0, self.coverage_width):

                        # Scale the bounding box to coverage map size
                        bx1 = (self.coverage_width * bbox_unscaled.x1 / self.resize_width)
                        bx2 = (self.coverage_width * bbox_unscaled.x2 / self.resize_width)

                        by1 = (self.coverage_height * bbox_unscaled.y1 / self.resize_height)
                        by2 = (self.coverage_height * bbox_unscaled.y2 / self.resize_height)

                        bbox = ia.BoundingBox(bx1, by1, bx2, by2)

                        for k in range(0, 9):

                            anchor_idx = y * self.coverage_height * 9 + x * 9 + k

                            iou = bbox.iou(self.anchors[anchor_idx])

                            if iou > 0.3:
                                if iou > output_region[i, y, x, k]:
                                        output_bboxes[i, int(y), int(x), 0] = bbox.x1 / self.coverage_width
                                        output_bboxes[i, int(y), int(x), 1] = bbox.y1 / self.coverage_height
                                        output_bboxes[i, int(y), int(x), 2] = bbox.x2 / self.coverage_width
                                        output_bboxes[i, int(y), int(x), 3] = bbox.y2 / self.coverage_height
                                        output_region[i, int(y), int(x), k] = iou

            for y in range(0, self.coverage_height):
                for x in range(0, self.coverage_width):
                    for k in range(0, 9):
                        if output_region[i, int(y), int(x), k] > 0.3:
                            output_region[i, int(y), int(x), k] = 1

        output_class = np.max(output_region, axis=-1).reshape((self.batch_size, 7, 7, 1))

        return input_image, [output_region, output_bboxes, output_class]


    @staticmethod
    # KITTI Format Labels
    def load_kitti_label(image: np.ndarray, scale, label: str):

        label = open(label, 'r').read()

        bboxes = []

        segmap = np.zeros((image.shape[0], image.shape[1], 3), dtype=np.uint8)

        for row in label.split('\n'):
            fields = row.split(' ')

            bbox_class = fields[0]

            # TODO: Can we use this information to generate more accurate segmentation maps or bboxes?
            bbox_truncated = float(fields[1])
            bbox_occluded = int(fields[2])
            bbox_alpha = float(fields[3])

            bbox_x1 = float(fields[4]) * scale[1]
            bbox_y1 = float(fields[5]) * scale[0]
            bbox_x2 = float(fields[6]) * scale[1]
            bbox_y2 = float(fields[7]) * scale[0]

            bbox = ia.BoundingBox(bbox_x1, bbox_y1, bbox_x2, bbox_y2, bbox_class)
            bboxes.append(bbox)

        bboi = ia.BoundingBoxesOnImage(bboxes, shape=image.shape)

        return bboi

    @staticmethod
    def create_augmenter(stage: str = "train"):
        if stage == "train":
            return iaa.Sequential([
                iaa.Fliplr(0.5),
                iaa.CropAndPad(px=(0, 112), sample_independently=False),
                iaa.Affine(translate_percent={"x": (-0.4, 0.4), "y": (-0.4, 0.4)}),
                iaa.SomeOf((0, 3), [
                    iaa.AddToHueAndSaturation((-10, 10)),
                    iaa.Affine(scale={"x": (0.9, 1.1), "y": (0.9, 1.1)}),
                    iaa.GaussianBlur(sigma=(0, 1.0)),
                    iaa.AdditiveGaussianNoise(scale=0.05 * 255)
                ])
            ])
        elif stage == "val":
            return iaa.Sequential([
                iaa.CropAndPad(px=(0, 112), sample_independently=False),
                iaa.Affine(translate_percent={"x": (-0.4, 0.4), "y": (-0.4, 0.4)}),
            ])
        elif stage == "test":
            return iaa.Sequential([])


@plac.annotations(
    batch_size=('The training batch size', 'option', 'B', int),
    epochs=('Number of epochs to train', 'option', 'E', int),
    train_path=(
            'Path to the train folder which contains both an images and labels folder with KITTI labels',
            'option', 'T', str),
    val_path=(
            'Path to the validation folder which contains both an images and labels folder with KITTI labels',
            'option', 'V', str),
    weights=('Weights file to start with', 'option', 'W', str),
    multi_gpu_weights=('Weights file to start with for the multi GPU model', 'option', 'G', str),
    workers=('Number of fit_generator workers', 'option', 'w', int),
    find_lr=('Instead of training, search for an optimal learning rate', 'flag', None, bool),
)
def main(batch_size: int = 24,
         epochs: int = 384,
         train_path: str = 'train',
         val_path: str = 'val',
         multi_gpu_weights=None,
         weights=None,
         workers: int = 8,
         find_lr: bool = False):

    keras_model = MobileDetectNetModel.complete_model()
    keras_model.summary()

    if weights is not None:
        keras_model.load_weights(weights, by_name=True)

    train_seq = MobileDetectNetSequence(train_path, stage="train", batch_size=batch_size)
    val_seq = MobileDetectNetSequence(val_path, stage="val", batch_size=batch_size)

    keras_model = keras.utils.multi_gpu_model(keras_model, gpus=[0, 1], cpu_merge=True, cpu_relocation=False)
    if multi_gpu_weights is not None:
        keras_model.load_weights(multi_gpu_weights, by_name=True)

    callbacks = []

    def region_loss(classes):
        def loss_fn(y_true, y_pred):
            # Don't penalize bounding box errors when there is no object present
            return 10*classes*K.abs(y_pred - y_true)
        return loss_fn

    keras_model.compile(optimizer=SGD(), loss=['mean_absolute_error',
                                               region_loss(keras_model.get_layer('classes').output),
                                               'binary_crossentropy'])

    if find_lr:
        from lr_finder import LRFinder
        lr_finder = LRFinder(keras_model)
        lr_finder.find_generator(train_seq, start_lr=0.000001, end_lr=1, epochs=5)
        lr_finder.plot_loss()
        return

    filepath = "weights-{epoch:02d}-{val_loss:.4f}-multi-gpu.hdf5"
    checkpoint = ModelCheckpoint(filepath, monitor='val_loss', verbose=1, save_best_only=True, mode='min')
    callbacks.append(checkpoint)

    sgdr_sched = SGDRScheduler(0.00001, 0.01, steps_per_epoch=np.ceil(len(train_seq) / batch_size), mult_factor=1.5)
    callbacks.append(sgdr_sched)

    keras_model.fit_generator(train_seq,
                              validation_data=val_seq,
                              epochs=epochs,
                              steps_per_epoch=np.ceil(len(train_seq) / batch_size),
                              validation_steps=np.ceil(len(val_seq) / batch_size),
                              callbacks=callbacks,
                              use_multiprocessing=True,
                              workers=workers,
                              shuffle=True)


if __name__ == '__main__':
    plac.call(main)
