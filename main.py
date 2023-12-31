import os
import argparse
import tensorflow as tf
from tqdm import tqdm
from models.model import SeqPAN
from utils.data_gen import gen_or_load_dataset
from utils.data_loader import TrainLoader, TestLoader, TrainNoSuffleLoader
from utils.data_utils import load_json, save_json, load_video_features
from utils.runner_utils import eval_test_save, get_feed_dict, write_tf_summary, set_tf_config, eval_test
from datetime import datetime

parser = argparse.ArgumentParser(description='parameters settings')
# data parameters
parser.add_argument('--save_dir', type=str, default='data_pkl', help='path to save processed dataset')
parser.add_argument('--task', type=str, default='anet', help='[anet | charades | charades_active | tacos]')
parser.add_argument('--fv', type=str, default='i3d', help='features')
parser.add_argument('--max_pos_len', type=int, default=100, help='maximal position sequence length allowed, '
                                                                 'anet: 100, charades: 64, tacos: 256')
# model parameters
parser.add_argument('--num_words', type=int, default=None, help='word dictionary size')
parser.add_argument('--num_chars', type=int, default=None, help='character dictionary size')
parser.add_argument('--word_dim', type=int, default=300, help='word embedding dimension')
parser.add_argument('--char_dim', type=int, default=100, help='character embedding dimension, anet: 100, '
                                                              'charades/tacos: 50')
parser.add_argument('--visual_dim', type=int, default=1024, help='video feature dimension [i3d: 1024 | c3d: 4096]')
parser.add_argument('--dim', type=int, default=128, help='hidden size of the model')
parser.add_argument('--num_heads', type=int, default=8, help='number of heads in transformer block')
parser.add_argument('--drop_rate', type=float, default=0.2, help='dropout rate')
parser.add_argument('--attn_layer', type=int, default=2, help='number of attention layers')
parser.add_argument('--match_lambda', type=float, default=1.0, help='weight of matching loss')
parser.add_argument('--tau', type=float, default=0.3, help='temperature of gumbel softmax')
parser.add_argument('--no_gumbel', action='store_true', help='whether use gumbel softmax')
# training/evaluation parameters
parser.add_argument('--gpu_idx', type=str, default='0', help='indicate which gpu is used')
parser.add_argument('--seed', type=int, default=12345, help='random seed')
parser.add_argument('--mode', type=str, default='train', help='[train | test]')
parser.add_argument('--epochs', type=int, default=50, help='maximal training epochs')
parser.add_argument('--num_train_steps', type=int, default=None, help='maximal training steps')
parser.add_argument('--batch_size', type=int, default=64, help='batch_size')
parser.add_argument("--clip_norm", type=float, default=1.0, help="gradient clip norm")
parser.add_argument("--init_lr", type=float, default=0.0002, help="initial learning rate")
parser.add_argument('--suffix', type=str, default="", help='saved checkpoint suffix')
configs = parser.parse_args()

# set tensorflow configs
set_tf_config(configs.seed, configs.gpu_idx)

if configs.task == 'charades':
    feature_path = "/storage_fast/rjliang/charades/feat/i3d_v0"
elif  configs.task == 'anet':
    feature_path = "/storage_fast/rjliang/activitynet/feat/i3d_v0"
elif  configs.task == 'tacos':
    feature_path = "/storage_fast/rjliang/tacos/i3d"     
else:
    raise
configs.feature_path = feature_path
configs.emb_path = '/storage_fast/rjliang/glove/glove.840B.300d.txt' 

# prepare or load dataset
dataset = gen_or_load_dataset(configs)
configs.num_chars = dataset['n_chars']
configs.num_words = dataset['n_words']

# get train and test loader

visual_features = load_video_features(feature_path, configs.max_pos_len)
train_loader = TrainLoader(dataset=dataset['train_set'], visual_features=visual_features, configs=configs)
test_loader = TestLoader(datasets=dataset, visual_features=visual_features, configs=configs)
train_nosuffle_loader = TrainNoSuffleLoader(datasets=dataset['train_set'], visual_features=visual_features, configs=configs)

home_dir = 'ckpt/{}/model_{}_{}'.format(configs.task, configs.fv, str(configs.max_pos_len))
if configs.suffix is not None:
    home_dir += '_' + configs.suffix
model_dir = os.path.join(home_dir, "model")

if configs.mode.lower() == 'train':
    eval_period = train_loader.num_batches()
    if not os.path.exists(model_dir):
        os.makedirs(model_dir)
    # write configs to json file
    save_json(vars(configs), filename=os.path.join(model_dir, "configs.json"), save_pretty=True)
    # create model and train
    with tf.Graph().as_default() as graph:
        model = SeqPAN(configs=configs, graph=graph, word_vectors=dataset['word_vector'])
        sess_config = tf.compat.v1.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        sess_config.gpu_options.allow_growth = True
        with tf.compat.v1.Session(config=sess_config) as sess:
            saver = tf.compat.v1.train.Saver(max_to_keep=1)
            # writer = tf.compat.v1.summary.FileWriter(log_dir)
            sess.run(tf.compat.v1.global_variables_initializer())
            best_r1i7 = -1.0
            date_str = datetime.now().strftime("%Y%m%d_%H%M%S") 
            score_writer = open(os.path.join(model_dir, date_str + "_eval_results.txt"), mode="w", encoding="utf-8")
            for epoch in range(configs.epochs):
                
                if epoch < 2:
                    cur_lr = 0.000001
                else:
                    cur_lr = configs.init_lr * (1.0 - epoch / configs.epochs)

                for data in tqdm(train_loader.batch_iter(), total=train_loader.num_batches(),
                                 desc='Epoch %d / %d' % (epoch + 1, configs.epochs)):
                    feed_dict = get_feed_dict(data, model, lr=cur_lr, drop_rate=configs.drop_rate, mode='train')

                    _, loss, loc_loss, mat_loss, global_step = sess.run(
                        [model.train_op, model.loss, model.loc_loss, model.match_loss,
                         model.global_step], feed_dict=feed_dict)

                    # print(tmp)
                    # print(tmp.shape, tmp.max(), tmp.min())
                    # print()

                    if global_step % 100 == 0:
                        value_pairs = [("train/loss", loss), ("train/loc_loss", loc_loss), ('train/mat_loss', mat_loss)]
                        # write_tf_summary(writer, value_pairs, global_step)
                    if global_step % eval_period == 0:  # evaluation

                        # r1i3, r1i5, r1i7, mi, value_pairs, score_str = eval_test(
                        #     sess=sess, model=model, data_loader=train_nosuffle_loader, epoch=epoch + 1, global_step=global_step)
                        # write_tf_summary(writer, value_pairs, global_step)
                        # score_writer.write(score_str)
                        # score_writer.flush()
                        # print('\nTRAIN Epoch: %2d | Step: %5d | r1i3: %.2f | r1i5: %.2f | r1i7: %.2f | mIoU: %.2f' % (
                        #     epoch + 1, global_step, r1i3, r1i5, r1i7, mi), flush=True)



                        r1i3, r1i5, r1i7, mi, value_pairs, score_str = eval_test(
                            sess=sess, model=model, data_loader=test_loader, epoch=epoch + 1, global_step=global_step)
                        # write_tf_summary(writer, value_pairs, global_step)
                        score_writer.write(score_str)
                        score_writer.flush()
                        print('\nTEST Epoch: %2d | Step: %5d | r1i3: %.2f | r1i5: %.2f | r1i7: %.2f | mIoU: %.2f' % (
                            epoch + 1, global_step, r1i3, r1i5, r1i7, mi), flush=True)
                       ## save the model according to the result of Rank@1, IoU=0.7
                        if mi > best_r1i7:
                            best_r1i7 = mi
                            filename = os.path.join(model_dir, "model_best.ckpt".format(global_step))
                            saver.save(sess, filename)
                            with open(model_dir+"/bestepoch", "w") as f:
                                f.write("{}".format(epoch+1))
                        print(best_r1i7)
            score_writer.close()

elif configs.mode.lower() in ['val', 'test']:
    if not os.path.exists(model_dir):
        raise ValueError('no pre-trained model exists!!!')
    pre_configs = load_json(os.path.join(model_dir, "configs.json"))
    parser.set_defaults(**pre_configs)
    configs = parser.parse_args()
    # load model and test
    with tf.Graph().as_default() as graph:
        model = SeqPAN(configs=configs, graph=graph, word_vectors=dataset['word_vector'])
        sess_config = tf.compat.v1.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        sess_config.gpu_options.allow_growth = True
        with tf.compat.v1.Session(config=sess_config) as sess:
            saver = tf.compat.v1.train.Saver()
            sess.run(tf.compat.v1.global_variables_initializer())
            saver.restore(sess, tf.train.latest_checkpoint(model_dir))
            r1i3, r1i5, r1i7, mi, *_ = eval_test(sess=sess, model=model, data_loader=test_loader, mode=configs.mode)
            print("\n" + "\x1b[1;31m" + "Rank@1, IoU=0.3:\t{:.2f}".format(r1i3) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "Rank@1, IoU=0.5:\t{:.2f}".format(r1i5) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "Rank@1, IoU=0.7:\t{:.2f}".format(r1i7) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "{}:\t{:.2f}".format("mean IoU".ljust(15), mi) + "\x1b[0m", flush=True)



elif configs.mode.lower() == "test_save":
    if not os.path.exists(model_dir):
        raise ValueError('no pre-trained model exists!!!')
    pre_configs = load_json(os.path.join(model_dir, "configs.json"))
    parser.set_defaults(**pre_configs)
    configs = parser.parse_args()
    # load model and test
    with tf.Graph().as_default() as graph:
        model = SeqPAN(configs=configs, graph=graph, word_vectors=dataset['word_vector'])
        sess_config = tf.compat.v1.ConfigProto(allow_soft_placement=True, log_device_placement=False)
        sess_config.gpu_options.allow_growth = True
        with tf.compat.v1.Session(config=sess_config) as sess:
            saver = tf.compat.v1.train.Saver()
            sess.run(tf.compat.v1.global_variables_initializer())
            saver.restore(sess, tf.train.latest_checkpoint(model_dir))
            r1i3, r1i5, r1i7, mi, *_ = eval_test_save(sess=sess, model=model, data_loader=train_nosuffle_loader, task=configs.task, suffix=configs.suffix)
            print("\n" + "\x1b[1;31m" + "Rank@1, IoU=0.3:\t{:.2f}".format(r1i3) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "Rank@1, IoU=0.5:\t{:.2f}".format(r1i5) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "Rank@1, IoU=0.7:\t{:.2f}".format(r1i7) + "\x1b[0m", flush=True)
            print("\x1b[1;31m" + "{}:\t{:.2f}".format("mean IoU".ljust(15), mi) + "\x1b[0m", flush=True)


else:
    raise ValueError("Unknown mode {}!!!".format(configs.mode))