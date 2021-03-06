import numpy as np
from PIL import Image
import tensorflow as tf
import matplotlib.pyplot as plt
from tensorflow.keras import backend as K
from tensorflow.keras import layers, models, optimizers


from utils import combine_images
from capsulelayers import CapsuleLayer, PrimaryCap, Length, Mask

K.set_image_data_format('channels_last')


def CapsNet(input_shape, n_class, routings, batch_size):
   
    x = layers.Input(shape=input_shape, batch_size=batch_size)
    # Layer 1: Just a conventional Conv2D layer
    conv1 = layers.Conv2D(filters=256, kernel_size=9, strides=1, padding='valid', activation='relu', name='conv1')(x)
    # Layer 2: Conv2D layer with `squash` activation, then reshape to [None, num_capsule, dim_capsule]
    primarycaps = PrimaryCap(conv1, dim_capsule=8, n_channels=32, kernel_size=9, strides=2, padding='valid')
    # Layer 3: Capsule layer. Routing algorithm works here.
    digitcaps = CapsuleLayer(num_capsule=n_class, dim_capsule=16, routings=routings, name='digitcaps')(primarycaps)
    # Layer 4: This is an auxiliary layer to replace each capsule with its length. Just to match the true label's shape.
    out_caps = Length(name='capsnet')(digitcaps)

    # Decoder network.
    y = layers.Input(shape=(n_class,),batch_size=batch_size)
    masked_by_y = Mask()([digitcaps, y])  # The true label is used to mask the output of capsule layer. For training
    masked = Mask()(digitcaps)  # Mask using the capsule with maximal length. For prediction

    # Shared Decoder model in training and prediction
    decoder = models.Sequential(name='decoder')
    decoder.add(layers.Dense(512, activation='relu', input_dim=16*n_class, batch_size=batch_size))
    decoder.add(layers.Dense(1024, activation='relu'))
    decoder.add(layers.Dense(np.prod(input_shape), activation='sigmoid'))
    decoder.add(layers.Reshape(target_shape=input_shape, name='out_recon'))

    # Models for training and evaluation (prediction)
    train_model = models.Model([x, y], [out_caps, decoder(masked_by_y)])
    eval_model = models.Model(x, [out_caps, decoder(masked)])

    # manipulate model
    noise = layers.Input(shape=(n_class, 16),batch_size=batch_size)
    noised_digitcaps = layers.Add()([digitcaps, noise])
    masked_noised_y = Mask()([noised_digitcaps, y])
    manipulate_model = models.Model([x, y, noise], decoder(masked_noised_y))

    return train_model, eval_model, manipulate_model


def margin_loss(y_true, y_pred):
    """
    Margin loss for Eq.(4). When y_true[i, :] contains not just one `1`, this loss should work too. Not test it.
    :param y_true: [None, n_classes]
    :param y_pred: [None, num_capsule]
    :return: a scalar loss value.
    """
    # return tf.reduce_mean(tf.square(y_pred))
    L = y_true * tf.square(tf.maximum(0., 0.9 - y_pred)) + \
        0.5 * (1 - y_true) * tf.square(tf.maximum(0., y_pred - 0.1))

    return tf.reduce_mean(tf.reduce_sum(L, 1))


def manipulate_latent(model, data, n_class, args):

    print(str('-'*30 + 'Begin: manipulate' + '-'*30).center(100))
    
    x_test, y_test = data

    index = np.argmax(y_test, 1) == args['sign']

    number = np.random.randint(low=0, high=sum(index) - 1)

    selected_indices = np.random.choice(len(y_test[index]), BATCH_SIZE, replace=True)
    print(selected_indices)

    x, y = x_test[index][selected_indices], y_test[index][selected_indices]

    noise = np.zeros([BATCH_SIZE, n_class, 16])
    x_recons = []
    for dim in range(16):
        for r in [-0.25, -0.2, -0.15, -0.1, -0.05, 0, 0.05, 0.1, 0.15, 0.2, 0.25]:
            tmp = np.copy(noise)
            tmp[:, :, dim] = r
            x_recon = model.predict([x, y, tmp])
            x_recons.append(x_recon)

    x_recons = np.concatenate(x_recons)

    img = combine_images(x_recons, height=16)
    image = img * 255
    Image.fromarray(image.astype(np.uint8)).save(args['output_path'] + '/manipulate-%d.png' % args['sign'])
    print(str('Manipulated result saved to %s/manipulate-%d.png' % (args['output_path'], args['sign'])).center(100))
    print(str('-'*30 + 'End: manipulate' + '-'*30).center(100))


if __name__ == "__main__":
    import os
    import time
    import glob2
    import numpy as np
    import cv2
    import argparse
    from tensorflow.keras.preprocessing.image import ImageDataGenerator
    from tensorflow.keras import callbacks
    from utils import load_dataset, split_dataset

    parser = argparse.ArgumentParser(description="Capsule Network on custom dataset.")
    
    # My custom optional arguments
    parser.add_argument('--data_path', default='../data', type=str,
                        help='The path of training image folder')
    parser.add_argument('--ratio', default=0.2, type=float,
                        help='The ratio splitting data into validation set and training set.')
    # End my custom optional arguments

    # Default optional arguments
    # setting the hyper parameters
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--batch_size', default=16, choices=[4, 8, 16, 32, 64, 128, 256], type=int)
    parser.add_argument('--lr', default=0.001, type=float,
                        help="Initial learning rate")
    parser.add_argument('--lr_decay', default=0.9, type=float,
                        help="The value multiplied by lr at each epoch. Set a larger value for larger epochs")
    parser.add_argument('--lam_recon', default=0.392, type=float,
                        help="The coefficient for the loss of decoder")
    parser.add_argument('-r', '--routings', default=3, type=int,
                        help="Number of iterations used in routing algorithm. should > 0")
    parser.add_argument('--shift_fraction', default=0.1, type=float,
                        help="Fraction of pixels to shift at most in each direction.")
    parser.add_argument('--save_dir', default='./result')
    parser.add_argument('-t', '--testing', action='store_true',
                        help="Test the trained model on testing dataset")
    parser.add_argument('-w', '--weights', default=None,
                        help="The path of the saved weights. Should be specified when testing")
    # End default optional arguments

    args = parser.parse_args()

    if not os.path.exists(args.save_dir):
        os.mkdir(args.save_dir)

    np.random.seed(18521489)
    # Load dataset
    X, y = load_dataset(args.data_path)
    (x_train, y_train), (x_test, y_test) = split_dataset(data=X, label=y, ratio=args.ratio)

    # define model
    model, eval_model, manipulate_model = CapsNet(input_shape=x_train.shape[1:],
                                                  n_class=len(np.unique(np.argmax(y_train, 1))),
                                                  routings=args.routings,
                                                  batch_size=args.batch_size)

    if not args.testing:
        if args.weights is not None:
            model.load_weights(args.weights)
        model.summary()
        train(model=model, data=((x_train, y_train), (x_test, y_test)), args=args)
    else:
        if args.weights is None:
            print('No weights are provided. Will test using random initialized weights.')
        else:
            #manipulate_model.load_weights(args.weights)
            eval_model.load_weights(args.weights)

        #manipulate_latent(model=manipulate_model, data=(x_test, y_test), 
        #                n_class=len(np.unique(np.argmax(y_train, 1))), args=args)
        test(model=eval_model, data=(x_test, y_test), args=args)
