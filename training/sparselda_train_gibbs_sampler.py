#!/usr/bin/env python
#coding=utf-8

# Copyright(c) 2013 python-sparselda project.
# Author: Lifeng Wang (ofandywang@gmail.com)

import logging
import os
import random
import sys

sys.path.append('..')
from common.document import Document
from common.model import Model
from common.ordered_sparse_topic_histogram import OrderedSparseTopicHistogram
from common.recordio import RecordReader
from common.recordio import RecordWriter
from common.vocabulary import Vocabulary

class SparseLDATrainGibbsSampler(object):
    """SparseLDATrainGibbsSampler implements the SparseLDA gibbs sampling
    training algorithm.  In gibbs sampling formula:

     (0) p(z|w) --> p(z|d) * p(w|z)
                --> (alpha(z) + N(z|d)) * p(w|z)
                 =  (alpha(z) + N(z|d)) * p(w|z) *
                    (beta + N(w|z)) / (beta * |V| + N(z))
                 =  alpha(z) * beta / (beta * |V| + N(z)) +
                    N(z|d) * beta / (beta * |V| + N(z)) +
                    (alpha(z) + N(z|d)) * N(w|z) / (beta * |V| + N(z))

     (1) s(z) = alpha(z) * beta / (beta * |V| + N(z))
     (2) r(z, d) = N(z|d) * beta / (beta * |V| + N(z))
     (3) q(z, w, d) = N(w|z) * (alpha(z) + N(z|d)) / (beta * |V| + N(z))

     (4) q_coefficient(z, d) = (alpha(z) + N(z|d)) / (beta * |V| + N(z))

    This process divides the full sampling mass into three buckets, where s(z)
    is a smoothing-only bucket, r(z, d) is a document-topic bucket, and
    q(z, w, d) is a topic-word bucket.

    The values of the three components of the normalization constant, s, r, q,
    can be efficiently calculated. The constant s only changes when update
    hyperparameters alpha(z). The constant r depends only on the document-topic
    counts, so we can calculate it once at the begining of each document and
    then update it by subtracting and adding values for the terms involving the
    old and new topic at each gibbs update. This process takes constant time,
    independent of the number of topics. The topic word constant q changes with
    the value of w, so we cannot as easily recycle earlier computation. We can,
    however, cache the coefficient q_coefficient for every topic, so calculating
    q for a given w consists of one multiply operation for every topic such that
    N(w|z) > 0.

    See 'Limin Yao, David Mimno, Andrew McCallum. Efficient methods for topic
    model inference on streaming document collections, In SIGKDD, 2009.' for
    more details.
    """

    def __init__(self, model, vocabulary):
        self.model = model
        self.vocabulary = vocabulary
        self.documents = []  # item fmt: common.lda_pb2.Document

        # s(z), smoothing only bucket, indexed by topic z.
        self.smoothing_only_bucket = [0 for i in xrange(self.model.num_topics)]
        self.smoothing_only_sum = 0.0

        # r(z, d), document-topic bucket, indexed by topic z.
        self.doc_topic_bucket = [0 for i in xrange(self.model.num_topics)]
        self.doc_topic_sum = 0.0

        # q(z, w, d), topic-word bucket, indexed by topic z.
        self.topic_word_bucket = [0 for i in xrange(self.model.num_topics)]
        self.topic_word_sum = 0.0
        # q_coefficient(z, d), indexed by topic z.
        self.topic_word_coef = [0 for i in xrange(self.model.num_topics)]

    def load_corpus(self, corpus_dir):
        """Load corpus from a given directory, then initialize the documents
        and model.
        Line format: token1 \t token2 \t token3 \t ... ...
        """
        del self.documents[:]
        self.model.global_topic_hist.Clear()
        for topic in xrange(self.model.num_topics):
            self.model.global_topic_hist.topic_counts.append(0)
        self.model.word_topic_hist.clear()

        rand = random.Random()

        for root, dirs, files in os.walk(corpus_dir):
            for f in files:
                filename = os.path.join(root, f)
                fp = open(filename, 'r')
                for doc_str in fp:
                    document = Document(self.model.num_topics)
                    document.parse_from_tokens(doc_str.split('\t'), rand, \
                            self.vocabulary)
                    # print "doc: %s" % document
                    if document.num_words() > 1:
                        self.documents.append(document)

                        for word in document.document_pb.words:
                            if word.id not in self.model.word_topic_hist:
                                self.model.word_topic_hist[word.id] = \
                                        OrderedSparseTopicHistogram(self.model.num_topics)
                            self.model.word_topic_hist[word.id].increase_topic( \
                                    word.topic, 1)
                            self.model.global_topic_hist.topic_counts[word.topic] += 1
                            print "testtestestesteeeeeeeeeeeeeeeeeeeeeee"

                fp.close()

        print 'model: %s' % str(self.model)
        self._calculate_smoothing_only_bucket()
        self._initialize_topic_word_coefficient()

    def save_model(self, model_dir):
        """Save lda model to model_dir.
        """
        self.model.save(model_dir)

    def save_checkpoint(self, checkpoint_dir, iteration):
        """Dump the corpus and current model as checkpoint.
        """
        if not os.path.exists(checkpoint_dir):
            os.mkdir(checkpoint_dir)
        checkpoint_dir += '/' + str(iteration)
        logging.info('Save checkpoint to %s.' % checkpoint_dir)
        if not os.path.exists(checkpoint_dir):
            os.mkdir(checkpoint_dir)

        # dump corpus
        corpus_dir = checkpoint_dir + '/corpus'
        os.mkdir(corpus_dir)
        c = 1
        fp = open(corpus_dir + '/documents.%d' % c, 'wb')
        record_writer = RecordWriter(fp)
        for document in self.documents:
            if c % 10000 == 0:
                fp.close()
                fp = open(corpus_dir + '/documents.%d' % c, 'wb')
                record_writer = RecordWriter(fp)
            record_writer.write(document.serialize_to())
            c += 1
        fp.close()

        # dump model
        self.save_model(checkpoint_dir + '/lda_model')

    def load_checkpoint(self, checkpoint_dir):
        """Load checkpoint form checkpoint_dir.
        """
        sub_dirs = os.listdir(checkpoint_dir)
        max_iteration = -1
        for sub_dir in os.listdir(checkpoint_dir):
            iteration = int(sub_dir)
            if iteration > max_iteration:
                max_iteration = iteration
        if max_iteration == -1:
            logging.warning('The checkpoint directory %s does not exists.' \
                    % checkpoint_dir)
            return None
        checkpoint_dir += str(max_iteration)
        logging.info('Load checkpoint from %s.' % checkpoint_dir)

        assert self._load_corpus(checkpoint_dir + '/corpus')
        assert self._load_model(checkpoint_dir + '/lda_model')
        return max_iteration

    def _load_corpus(self, corpus_dir):
        del self.documents[:]
        if not os.path.exists(corpus_dir):
            logging.error('The corpus directory %s does not exists.' \
                    % corpus_dir)
            return False

        for root, dirs, files in os.walk(corpus_dir):
            for f in files:
                filename = os.path.join(root, f)
                fp = open(filename, 'rb')
                record_reader = RecordReader(fp)
                while True:
                    blob = record_reader.read()
                    if blob == None:
                        break
                    document = Document(self.model.num_topics)
                    document.parse_from_string(blob)
                    self.documents.append(document)

        return True

    def _load_model(self, model_dir):
        if not os.path.exists(model_dir):
            logging.error('The lda model directory %s does not exists.' \
                    % model_dir)
            return False
        self.model.load(model_dir)
        return True

    def gibbs_sampling(self, rand):
        """Perform one iteration of Gibbs Sampling.
        """
        for document in self.documents:
            self._calculate_doc_topic_bucket(document)
            self._update_topic_word_coefficient(document)
            print 'document old: ' + str(document)
            for i in xrange(len(document.document_pb.words)):
                word = document.document_pb.words[i]
                self._remove_word_topic(document, word)
                self._calculate_topic_word_bucket(word)
                new_topic = self._sample_new_topic(document, word, rand)
                word.topic = new_topic
                self._add_word_topic(document, word)
            print 'document new: ' + str(document)
            self._reset_topic_word_coefficient(document)

    def _calculate_smoothing_only_bucket(self):
        """s(z) = alpha(z) * beta / (beta * |V| + N(z))
        """
        self.smoothing_only_sum = 0.0
        for topic in xrange(self.model.num_topics):
            self.smoothing_only_bucket[topic] = \
                    self.model.hyper_params.topic_prior * \
                    self.model.hyper_params.word_prior / \
                    (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                    self.model.global_topic_hist.topic_counts[topic])
            self.smoothing_only_sum += self.smoothing_only_bucket[topic]

    def _calculate_doc_topic_bucket(self, document):
        """r(z, d) = N(z|d) * beta / (beta * |V| + N(z))
        """
        self.doc_topic_sum = 0.0
        self.doc_topic_bucket = [0 for i in xrange(self.model.num_topics)]
        for non_zero in document.doc_topic_hist.sparse_topic_hist.non_zeros:
            self.doc_topic_bucket[non_zero.topic] = \
                    document.get_topic_count(non_zero.topic) * \
                    self.model.hyper_params.word_prior / \
                    (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                    self.model.global_topic_hist.topic_counts[non_zero.topic])
            self.doc_topic_sum += self.doc_topic_bucket[non_zero.topic]

    def _initialize_topic_word_coefficient(self):
        """q_coefficient(z) = alpha(z) / (beta * |V| + N(z)),
        """
        for topic in xrange(self.model.num_topics):
            self.topic_word_coef[topic] = \
                    self.model.hyper_params.topic_prior / \
                    (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                    self.model.global_topic_hist.topic_counts[topic])

    def _update_topic_word_coefficient(self, document):
        """q_coefficient(z, d) = (alpha(z) + N(z|d)) / (beta * |V| + N(z))
        """
        for non_zero in document.doc_topic_hist.sparse_topic_hist.non_zeros:
            self.topic_word_coef[non_zero.topic] = \
                    (self.model.hyper_params.topic_prior + non_zero.count) / \
                    (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                    self.model.global_topic_hist.topic_counts[non_zero.topic])

    def _reset_topic_word_coefficient(self, document):
        """q_coefficient(z) = alpha(z) / (beta * |V| + N(z)),
        """
        for non_zero in document.doc_topic_hist.sparse_topic_hist.non_zeros:
            self.topic_word_coef[non_zero.topic] = \
                    self.model.hyper_params.topic_prior / \
                    (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                    self.model.global_topic_hist.topic_counts[non_zero.topic])

    def _calculate_topic_word_bucket(self, word):
        """q(z, w, d) = N(w|z) * (alpha(z) + N(z|d)) / (beta * |V| + N(z))
                      = N(w|z) * q_coefficient(z, d)
        """
        self.topic_word_sum = 0.0
        ordered_sparse_topic_hist = self.model.word_topic_hist[word.id]
        for non_zero in ordered_sparse_topic_hist.sparse_topic_hist.non_zeros:
            self.topic_word_bucket[non_zero.topic] = \
                    non_zero.count * self.topic_word_coef[non_zero.topic]
            self.topic_word_sum += self.topic_word_bucket[non_zero.topic]

    def _remove_word_topic(self, document, word):
        self.model.global_topic_hist.topic_counts[word.topic] -= 1
        self.model.word_topic_hist[word.id].decrease_topic(word.topic, 1)

        self.smoothing_only_sum -= self.smoothing_only_bucket[word.topic]
        self.doc_topic_sum -= self.doc_topic_bucket[word.topic]
        document.decrease_topic(word.topic, 1)

        self.smoothing_only_bucket[word.topic] = \
                self.model.hyper_params.topic_prior * \
                self.model.hyper_params.word_prior / \
                (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                self.model.global_topic_hist.topic_counts[word.topic])
        self.smoothing_only_sum += self.smoothing_only_bucket[word.topic]

        self.doc_topic_bucket[word.topic] = \
                document.get_topic_count(word.topic) * \
                self.model.hyper_params.word_prior / \
                (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                self.model.global_topic_hist.topic_counts[word.topic])
        self.doc_topic_sum += self.doc_topic_bucket[word.topic]

        self.topic_word_coef[word.topic] = \
                (self.model.hyper_params.topic_prior + \
                document.get_topic_count(word.topic)) / \
                (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                self.model.global_topic_hist.topic_counts[word.topic])

    def _add_word_topic(self, document, word):
        self.model.global_topic_hist.topic_counts[word.topic] += 1
        self.model.word_topic_hist[word.id].increase_topic(word.topic, 1)

        self.smoothing_only_sum -= self.smoothing_only_bucket[word.topic]
        self.doc_topic_sum -= self.doc_topic_bucket[word.topic]
        document.increase_topic(word.topic, 1)

        self.smoothing_only_bucket[word.topic] = \
                self.model.hyper_params.topic_prior * \
                self.model.hyper_params.word_prior / \
                (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                self.model.global_topic_hist.topic_counts[word.topic])
        self.smoothing_only_sum += self.smoothing_only_bucket[word.topic]

        self.doc_topic_bucket[word.topic] = \
                document.get_topic_count(word.topic) * \
                self.model.hyper_params.word_prior / \
                (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                self.model.global_topic_hist.topic_counts[word.topic])
        self.doc_topic_sum += self.doc_topic_bucket[word.topic]

        self.topic_word_coef[word.topic] = \
                (self.model.hyper_params.topic_prior + \
                document.get_topic_count(word.topic)) / \
                (self.model.hyper_params.word_prior * self.vocabulary.size() + \
                self.model.global_topic_hist.topic_counts[word.topic])

    def _sample_new_topic(self, document, word, rand):
        """Sampling a new topic for current word.

        Returns the new topic.
        """
        total_mass = self.smoothing_only_sum + self.doc_topic_sum + \
                self.topic_word_sum
        sample = rand.uniform(0.0, total_mass)

        # In general, self.topic_word_sum >> self.smoothing_only_sum
        # self.topic_word_sum >> self.doc_topic_sum
        if sample < self.topic_word_sum:
            ordered_sparse_topic_hist = self.model.word_topic_hist[word.id]
            for non_zero in ordered_sparse_topic_hist.sparse_topic_hist.non_zeros:
                sample -= self.topic_word_bucket[non_zero.topic]
                if sample <= 0:
                    return non_zero.topic
        else:
            sample -= self.topic_word_sum
            # self.doc_topic_bucket is sparse.
            if sample < self.doc_topic_sum:
                for non_zero in document.doc_topic_hist.sparse_topic_hist.non_zeros:
                    sample -= self.doc_topic_bucket[non_zero.topic]
                    if sample <= 0:
                        return non_zero.topic
            else:
                sample -= self.doc_topic_sum
                for topic, value in enumerate(self.smoothing_only_bucket):
                    sample -= value
                    if sample <= 0:
                        return topic

        logging.error('Sampling word topic failed.')
        return None

