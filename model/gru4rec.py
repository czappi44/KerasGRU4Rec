import tensorflow as tf
import tensorflow.keras as keras
import tensorflow.keras.backend as K
from tensorflow.keras.models import Model
from tensorflow.keras.utils import to_categorical
from tensorflow.keras.callbacks import ModelCheckpoint
from tensorflow.keras.losses import categorical_crossentropy
from tensorflow.keras.layers import Input, Dense, Dropout, GRU

import numpy as np
import argparse
import pandas as pd
from tqdm import tqdm
import time


class SessionDataset:
    """Credit to yhs-968/pyGRU4REC."""
    def __init__(self, data, sep='\t', session_key='SessionId', item_key='ItemId', time_key='Time', n_samples=-1, itemmap=None, time_sort=False):
        """
        Args:
            path: path of the csv file
            sep: separator for the csv
            session_key, item_key, time_key: name of the fields corresponding to the sessions, items, time
            n_samples: the number of samples to use. If -1, use the whole dataset.
            itemmap: mapping between item IDs and item indices
            time_sort: whether to sort the sessions by time or not
        """
        self.df = data
        self.session_key = session_key
        self.item_key = item_key
        self.time_key = time_key
        self.time_sort = time_sort
        self.add_item_indices(itemmap=itemmap)
        self.df.sort_values([session_key, time_key], inplace=True)

        # Sort the df by time, and then by session ID. That is, df is sorted by session ID and
        # clicks within a session are next to each other, where the clicks within a session are time-ordered.

        self.click_offsets = self.get_click_offsets()
        self.session_idx_arr = self.order_session_idx()

    def get_click_offsets(self):
        """
        Return the offsets of the beginning clicks of each session IDs,
        where the offset is calculated against the first click of the first session ID.
        """
        offsets = np.zeros(self.df[self.session_key].nunique() + 1, dtype=np.int32)
        # group & sort the df by session_key and get the offset values
        offsets[1:] = self.df.groupby(self.session_key).size().cumsum()

        return offsets

    def order_session_idx(self):
        """ Order the session indices """
        if self.time_sort:
            # starting time for each sessions, sorted by session IDs
            sessions_start_time = self.df.groupby(self.session_key)[self.time_key].min().values
            # order the session indices by session starting times
            session_idx_arr = np.argsort(sessions_start_time)
        else:
            session_idx_arr = np.arange(self.df[self.session_key].nunique())

        return session_idx_arr

    def add_item_indices(self, itemmap=None):
        """
        Add item index column named "item_idx" to the df
        Args:
            itemmap (pd.DataFrame): mapping between the item Ids and indices
        """
        if itemmap is None:
            item_ids = self.df[self.item_key].unique()  # unique item ids
            item2idx = pd.Series(data=np.arange(len(item_ids)),
                                 index=item_ids)
            itemmap = pd.DataFrame({self.item_key:item_ids,
                                   'item_idx':item2idx[item_ids].values})

        self.itemmap = itemmap
        self.df = pd.merge(self.df, self.itemmap, on=self.item_key, how='inner')

    @property
    def items(self):
        return self.itemmap.ItemId.unique()


class SessionDataLoader:
    """Credit to yhs-968/pyGRU4REC."""
    def __init__(self, dataset, batch_size=50, use_correct_mask_reset=False):
        """
        A class for creating session-parallel mini-batches.
        Args:
            dataset (SessionDataset): the session dataset to generate the batches from
            batch_size (int): size of the batch
        """
        self.dataset = dataset
        self.batch_size = batch_size
        self.done_sessions_counter = 0
        self.use_correct_mask_reset = use_correct_mask_reset

    def __iter__(self):
        """ Returns the iterator for producing session-parallel training mini-batches.
        Yields:
            input (B,):  Item indices that will be encoded as one-hot vectors later.
            target (B,): a Variable that stores the target item indices
            masks: Numpy array indicating the positions of the sessions to be terminated
        """

        df = self.dataset.df
        session_key='SessionId'
        item_key='ItemId'
        time_key='TimeStamp'
        self.n_items = df[item_key].nunique()+1
        click_offsets = self.dataset.click_offsets
        session_idx_arr = self.dataset.session_idx_arr

        iters = np.arange(self.batch_size)
        maxiter = iters.max()
        start = click_offsets[session_idx_arr[iters]]
        end = click_offsets[session_idx_arr[iters] + 1]
        mask = [] # indicator for the sessions to be terminated
        finished = False

        while not finished:
            minlen = (end - start).min()
            # Item indices (for embedding) for clicks where the first sessions start
            idx_target = df.item_idx.values[start]
            for i in range(minlen - 1):
                # Build inputs & targets
                idx_input = idx_target
                idx_target = df.item_idx.values[start + i + 1]
                inp = idx_input
                target = idx_target
                yield inp, target, mask
                if (i == 0) and self.use_correct_mask_reset:
                    mask = []

            # click indices where a particular session meets second-to-last element
            start = start + (minlen - 1)
            # see if how many sessions should terminate
            mask = np.arange(len(iters))[(end - start) <= 1]
            self.done_sessions_counter = len(mask)
            for idx in mask:
                maxiter += 1
                if maxiter >= len(click_offsets) - 1:
                    finished = True
                    break
                # update the next starting/ending point
                iters[idx] = maxiter
                start[idx] = click_offsets[session_idx_arr[maxiter]]
                end[idx] = click_offsets[session_idx_arr[maxiter] + 1]


def create_model(args):
    inputs = Input(batch_shape=(args.batch_size, 1, args.train_n_items))
    gru, gru_states = GRU(args.hidden_size, stateful=True, return_state=True, name="GRU")(inputs)
    drop2 = Dropout(args.dropout_p_hidden)(gru)
    predictions = Dense(args.train_n_items, activation='softmax')(drop2)
    model = Model(inputs=inputs, outputs=[predictions])
    if args.optim == "adam":
        opt = tf.keras.optimizers.Adam(lr=0.001, beta_1=0.9, beta_2=0.999, epsilon=None, decay=0.0, amsgrad=False)
    elif args.optim == "adagrad":
        opt = tf.keras.optimizers.Adagrad(lr=args.lr, epsilon=1e-6, initial_accumulator_value=0.0)
    else:
        raise ValueError(f"Invalid optimizer type: {args.optim}")
    model.compile(loss=categorical_crossentropy, optimizer=opt)#, run_eagerly=True)
    model.summary()

    filepath='./model_checkpoint.h5'
    checkpoint = ModelCheckpoint(filepath, monitor='loss', verbose=2, save_best_only=True, mode='min')
    callbacks_list = []
    return model


def get_metrics(model, args, train_generator_map, recall_k=20, mrr_k=20):

    test_dataset = SessionDataset(args.test_data, itemmap=train_generator_map)
    test_generator = SessionDataLoader(test_dataset, batch_size=args.batch_size, use_correct_mask_reset=args.use_correct_mask_reset)

    n = 0
    rec_sum = 0
    mrr_sum = 0

    print("Evaluating model...")
    for feat, label, mask in test_generator:

        gru_layer = model.get_layer(name="GRU")
        hidden_states = gru_layer.states[0].numpy()
        for elt in mask:
            hidden_states[elt, :] = 0
        gru_layer.reset_states(states=hidden_states)

        target_oh = to_categorical(label, num_classes=args.train_n_items)
        input_oh  = to_categorical(feat,  num_classes=args.train_n_items)
        input_oh = np.expand_dims(input_oh, axis=1)

        pred = model.predict(input_oh, batch_size=args.batch_size)

        for row_idx in range(feat.shape[0]):
            pred_row = pred[row_idx]
            label_row = target_oh[row_idx]

            rec_idx =  pred_row.argsort()[-recall_k:][::-1]
            mrr_idx =  pred_row.argsort()[-mrr_k:][::-1]
            tru_idx = label_row.argsort()[-1:][::-1]

            n += 1

            if tru_idx[0] in rec_idx:
                rec_sum += 1

            if tru_idx[0] in mrr_idx:
                mrr_sum += 1/int((np.where(mrr_idx == tru_idx[0])[0]+1))

    recall = rec_sum/n
    mrr = mrr_sum/n
    return (recall, recall_k), (mrr, mrr_k)

def train_model(model, args):
    train_dataset = SessionDataset(args.train_data)
    model_to_train = model
    batch_size = args.batch_size

    for epoch in range(args.epochs):
        epoch_start = time.time()
        losses = []
        events = []
        with tqdm(total=args.train_samples_qty) as pbar:
            loader = SessionDataLoader(train_dataset, batch_size=batch_size, use_correct_mask_reset=args.use_correct_mask_reset)
            for feat, target, mask in loader:
                gru_layer = model_to_train.get_layer(name="GRU")
                hidden_states = gru_layer.states[0].numpy()
                for elt in mask:
                    hidden_states[elt, :] = 0
                gru_layer.reset_states(states=hidden_states)
                
                input_oh = to_categorical(feat, num_classes=loader.n_items)
                input_oh = np.expand_dims(input_oh, axis=1)
                target_oh = to_categorical(target, num_classes=loader.n_items)

                tr_loss = model_to_train.train_on_batch(input_oh, target_oh)
                losses.append(tr_loss)  
                events.append(len(feat))                   
                pbar.set_description("Epoch {0}. Loss: {1:.5f}".format(epoch, tr_loss))
                pbar.update(loader.done_sessions_counter)

        epoch_duration = time.time() - epoch_start
        print(f"epoch:{epoch} loss: {np.mean(losses):.6f} {epoch_duration:.2f} s {np.sum(events)/epoch_duration:.2f} e/s {len(events)/epoch_duration:.2f} mb/s")
        if args.save_weights:
            print("Saving weights...")
            model_to_train.save(f"{args.save_path}/GRU4REC_{epoch}.h5")

        if args.eval_all_epochs:
            (rec, rec_k), (mrr, mrr_k) = get_metrics(model_to_train, args, train_dataset.itemmap)
            print("\t - Recall@{} epoch {}: {:8f}".format(rec_k, epoch, rec))
            print("\t - MRR@{}    epoch {}: {:8f}\n".format(mrr_k, epoch, mrr))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Keras GRU4REC: session-based recommendations')
    parser.add_argument('--resume', type=str, help='stored model path to continue training')
    parser.add_argument('--train_path', type=str, default='../../processedData/rsc15_train_tr.txt')
    parser.add_argument('--eval_only', type=bool, default=False)
    parser.add_argument('--test_path', type=str, default='../../processedData/rsc15_test.txt')
    parser.add_argument('--save_path', type=str, default='')
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--optim', type=str, default='adam')
    parser.add_argument('--hidden_size', type=int, default=100)
    parser.add_argument('--batch_size', type=int, default=512)
    parser.add_argument('--dropout_p_hidden', type=float, default=0.0)
    parser.add_argument('--eval_all_epochs', type=bool, default=False)
    parser.add_argument('--save_weights', type=bool, default=True)
    parser.add_argument('--epochs', type=int, default=10)
    parser.add_argument('--m', '--measure', type=int, nargs='+', default=[20])
    parser.add_argument('--use_correct_mask_reset', default=False, action='store_true')

    args = parser.parse_args()
    print(pd.DataFrame({'Args':list(args.__dict__.keys()), 'Values':list(args.__dict__.values())}))

    args.train_data = pd.read_csv(args.train_path, sep='\t', dtype={'ItemId': np.int64})
    args.test_data  = pd.read_csv(args.test_path,  sep='\t', dtype={'ItemId': np.int64})

    args.train_n_items = len(args.train_data['ItemId'].unique()) + 1

    args.train_samples_qty = len(args.train_data['SessionId'].unique()) + 1
    args.test_samples_qty = len(args.test_data['SessionId'].unique()) + 1

    if args.resume:
        try:
            model = keras.models.load_model(args.resume)
            print("Model checkpoint '{}' loaded!".format(args.resume))
        except OSError:
            print("Model checkpoint could not be loaded. Training from scratch...")
            model = create_model(args)
    else:
        model = create_model(args)

    if args.eval_only:
        train_dataset = SessionDataset(args.train_data)
        for k in args.m:
            (rec, rec_k), (mrr, mrr_k) = get_metrics(model, args, train_dataset.itemmap, recall_k=k, mrr_k=k)
            print(f'Recall@{k}: {rec:.8} MRR@{k}: {mrr:.8}')
    else:
        train_model(model, args)
