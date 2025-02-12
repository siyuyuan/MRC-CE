#! -*- coding: utf-8 -*-
# 自定义层
import os
import numpy as np
import tensorflow as tf
from tensorflow import keras
import tensorflow.keras.backend as K
from tensorflow.keras import initializers, activations
from tensorflow.keras.layers import *

do_recompute = int(os.environ.get('RECOMPUTE', '0'))

def sequence_masking(x, mask, mode=0, axis=None):
	"""为序列条件mask的函数
	mask: 形如(batch_size, seq_len)的0-1矩阵；
	mode: 如果是0，则直接乘以mask；
		  如果是1，则在padding部分减去一个大正数。
	axis: 序列所在轴，默认为1；
	"""
	if mask is None or mode not in [0, 1]:
		return x
	else:
		if axis is None:
			axis = 1
		if axis == -1:
			axis = K.ndim(x) - 1
		assert axis > 0, 'axis muse be greater than 0'
		for _ in range(axis - 1):
			mask = K.expand_dims(mask, 1)
		for _ in range(K.ndim(x) - K.ndim(mask) - axis + 1):
			mask = K.expand_dims(mask, K.ndim(mask))
		if mode == 0:
			return x * mask
		else:
			return x - (1 - mask) * 1e12

def recompute_grad(call):
	"""重计算装饰器（用来装饰Keras层的call函数）
	关于重计算，请参考：https://arxiv.org/abs/1604.06174
	"""
	if not do_recompute: return call

	def inner(self, inputs, **kwargs):
		"""定义需要求梯度的函数以及重新定义求梯度过程
		（参考自官方自带的tf.recompute_grad函数）
		"""
		flat_inputs = nest.flatten(inputs)
		call_args = tf_inspect.getfullargspec(call).args
		for key in ['mask', 'training']:
			if key not in call_args and key in kwargs:
				del kwargs[key]

		def kernel_call():
			"""定义前向计算
			"""
			return call(self, inputs, **kwargs)

		def call_and_grad(*inputs):
			"""定义前向计算和反向计算
			"""
			if is_tf_keras:
				with tape.stop_recording():
					outputs = kernel_call()
					outputs = tf.identity(outputs)
			else:
				outputs = kernel_call()

			def grad_fn(doutputs, variables=None):
				watches = list(inputs)
				if variables is not None:
					watches += list(variables)
				with tf.GradientTape() as t:
					t.watch(watches)
					with tf.control_dependencies([doutputs]):
						outputs = kernel_call()
				grads = t.gradient(
					outputs, watches, output_gradients=[doutputs]
				)
				del t
				return grads[:len(inputs)], grads[len(inputs):]

			return outputs, grad_fn

		if is_tf_keras:  # 仅在tf >= 2.0下可用
			outputs, grad_fn = call_and_grad(*flat_inputs)
			flat_outputs = nest.flatten(outputs)

			def actual_grad_fn(*doutputs):
				grads = grad_fn(*doutputs, variables=self.trainable_weights)
				return grads[0] + grads[1]

			watches = flat_inputs + self.trainable_weights
			watches = [tf.convert_to_tensor(x) for x in watches]
			tape.record_operation(
				call.__name__, flat_outputs, watches, actual_grad_fn
			)
			return outputs
		else:  # keras + tf >= 1.14 均可用
			return graph_mode_decorator(call_and_grad, *flat_inputs)

	return inner


def integerize_shape(func):
	"""装饰器，保证input_shape一定是int或None
	"""
	def convert(item):
		if hasattr(item, '__iter__'):
			return [convert(i) for i in item]
		elif hasattr(item, 'value'):
			return item.value
		else:
			return item

	def new_func(self, input_shape):
		input_shape = convert(input_shape)
		return func(self, input_shape)

	return new_func


class Layer(keras.layers.Layer):
	def __init__(self, **kwargs):
		super(Layer, self).__init__(**kwargs)
		self.supports_masking = True  # 本项目的自定义层均可mask


class Embedding(keras.layers.Embedding):
	"""拓展Embedding层
	"""
	def compute_mask(self, inputs, mask=None):
		"""为了适配T5，保证第一个token不被mask
		"""
		if self._current_mode == 'embedding':
			mask = super(Embedding, self).compute_mask(inputs, mask)
			if mask is not None:
				mask1 = K.ones_like(mask[:, :1], dtype='bool')
				mask2 = mask[:, 1:]
				return K.concatenate([mask1, mask2], 1)
		else:
			return mask

	def call(self, inputs, mode='embedding'):
		"""新增mode参数，可以为embedding或dense。如果为embedding，
		则等价于普通Embedding层；如果为dense，则等价于无bias的Dense层。
		"""
		self._current_mode = mode
		if mode == 'embedding':
			return super(Embedding, self).call(inputs)
		else:
			kernel = K.transpose(self.embeddings)
			return K.dot(inputs, kernel)

	def compute_output_shape(self, input_shape):
		if self._current_mode == 'embedding':
			return super(Embedding, self).compute_output_shape(input_shape)
		else:
			return input_shape[:2] + (K.int_shape(self.embeddings)[0],)


class BiasAdd(Layer):
	"""加上偏置项
	"""
	@integerize_shape
	def build(self, input_shape):
		super(BiasAdd, self).build(input_shape)
		output_dim = input_shape[-1]
		self.bias = self.add_weight(
			name='bias',
			shape=(output_dim,),
			initializer='zeros',
			trainable=True
		)

	def call(self, inputs):
		return K.bias_add(inputs, self.bias)


class MultiHeadAttention(Layer):
	"""多头注意力机制
	"""
	def __init__(
		self,
		heads,
		head_size,
		key_size=None,
		use_bias=True,
		attention_scale=True,
		kernel_initializer='glorot_uniform',
		**kwargs
	):
		super(MultiHeadAttention, self).__init__(**kwargs)
		self.heads = heads
		self.head_size = head_size
		self.out_dim = heads * head_size
		self.key_size = key_size or head_size
		self.use_bias = use_bias
		self.attention_scale = attention_scale
		self.kernel_initializer = initializers.get(kernel_initializer)

	def build(self, input_shape):
		super(MultiHeadAttention, self).build(input_shape)
		self.q_dense = Dense(
			units=self.key_size * self.heads,
			use_bias=self.use_bias,
			kernel_initializer=self.kernel_initializer
		)
		self.k_dense = Dense(
			units=self.key_size * self.heads,
			use_bias=self.use_bias,
			kernel_initializer=self.kernel_initializer
		)
		self.v_dense = Dense(
			units=self.out_dim,
			use_bias=self.use_bias,
			kernel_initializer=self.kernel_initializer
		)
		self.o_dense = Dense(
			units=self.out_dim,
			use_bias=self.use_bias,
			kernel_initializer=self.kernel_initializer
		)

	@recompute_grad
	def call(self, inputs, mask=None, a_mask=None, p_bias=None):
		"""实现多头注意力
		q_mask: 对输入的query序列的mask。
				主要是将输出结果的padding部分置0。
		v_mask: 对输入的value序列的mask。
				主要是防止attention读取到padding信息。
		a_mask: 对attention矩阵的mask。
				不同的attention mask对应不同的应用。
		p_bias: 在attention里的位置偏置。
				一般用来指定相对位置编码的种类。
		"""
		q, k, v = inputs[:3]
		q_mask, v_mask, n = None, None, 3
		if mask is not None:
			if mask[0] is not None:
				q_mask = K.cast(mask[0], K.floatx())
			if mask[2] is not None:
				v_mask = K.cast(mask[2], K.floatx())
		if a_mask:
			a_mask = inputs[n]
			n += 1
		# 线性变换
		qw = self.q_dense(q)
		kw = self.k_dense(k)
		vw = self.v_dense(v)
		# 形状变换
		qw = K.reshape(qw, (-1, K.shape(q)[1], self.heads, self.key_size))
		kw = K.reshape(kw, (-1, K.shape(k)[1], self.heads, self.key_size))
		vw = K.reshape(vw, (-1, K.shape(v)[1], self.heads, self.head_size))
		# Attention
		a = tf.einsum('bjhd,bkhd->bhjk', qw, kw)
		# 处理位置编码
		if p_bias == 'typical_relative':
			pos_embeddings = inputs[n]
			a = a + tf.einsum('bjhd,jkd->bhjk', qw, pos_embeddings)
		elif p_bias == 't5_relative':
			pos_embeddings = K.permute_dimensions(inputs[n], (2, 0, 1))
			a = a + K.expand_dims(pos_embeddings, 0)
		# Attention（续）
		if self.attention_scale:
			a = a / self.key_size**0.5
		a = sequence_masking(a, v_mask, 1, -1)
		if a_mask is not None:
			a = a - (1 - a_mask) * 1e12
		a = K.softmax(a)
		# 完成输出
		o = tf.einsum('bhjk,bkhd->bjhd', a, vw)
		if p_bias == 'typical_relative':
			o = o + tf.einsum('bhjk,jkd->bjhd', a, pos_embeddings)
		o = K.reshape(o, (-1, K.shape(o)[1], self.out_dim))
		o = self.o_dense(o)
		# 返回结果
		o = sequence_masking(o, q_mask, 0)
		return o

	def compute_output_shape(self, input_shape):
		return (input_shape[0][0], input_shape[0][1], self.out_dim)

	def compute_mask(self, inputs, mask=None):
		if mask is not None:
			return mask[0]

	def get_config(self):
		config = {
			'heads': self.heads,
			'head_size': self.head_size,
			'key_size': self.key_size,
			'use_bias': self.use_bias,
			'attention_scale': self.attention_scale,
			'kernel_initializer':
				initializers.serialize(self.kernel_initializer),
		}
		base_config = super(MultiHeadAttention, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class LayerNormalization(Layer):
	"""(Conditional) Layer Normalization
	hidden_*系列参数仅为有条件输入时(conditional=True)使用
	"""
	def __init__(
		self,
		center=True,
		scale=True,
		epsilon=None,
		conditional=False,
		hidden_units=None,
		hidden_activation='linear',
		hidden_initializer='glorot_uniform',
		**kwargs
	):
		super(LayerNormalization, self).__init__(**kwargs)
		self.center = center
		self.scale = scale
		self.conditional = conditional
		self.hidden_units = hidden_units
		self.hidden_activation = activations.get(hidden_activation)
		self.hidden_initializer = initializers.get(hidden_initializer)
		self.epsilon = epsilon or 1e-12

	def compute_mask(self, inputs, mask=None):
		if self.conditional:
			masks = [K.expand_dims(m, 0) for m in mask if m is not None]
			if len(masks) == 0:
				return None
			else:
				return K.all(K.concatenate(masks, axis=0), axis=0)
		else:
			return mask

	def build(self, input_shape):
		super(LayerNormalization, self).build(input_shape)

		if self.conditional:
			shape = (input_shape[0][-1],)
		else:
			shape = (input_shape[-1],)

		if self.center:
			self.beta = self.add_weight(
				shape=shape, initializer='zeros', name='beta'
			)
		if self.scale:
			self.gamma = self.add_weight(
				shape=shape, initializer='ones', name='gamma'
			)

		if self.conditional:

			if self.hidden_units is not None:
				self.hidden_dense = Dense(
					units=self.hidden_units,
					activation=self.hidden_activation,
					use_bias=False,
					kernel_initializer=self.hidden_initializer
				)

			if self.center:
				self.beta_dense = Dense(
					units=shape[0], use_bias=False, kernel_initializer='zeros'
				)
			if self.scale:
				self.gamma_dense = Dense(
					units=shape[0], use_bias=False, kernel_initializer='zeros'
				)

	@recompute_grad
	def call(self, inputs):
		"""如果是条件Layer Norm，则默认以list为输入，第二个是condition
		"""
		if self.conditional:
			inputs, cond = inputs
			if self.hidden_units is not None:
				cond = self.hidden_dense(cond)
			for _ in range(K.ndim(inputs) - K.ndim(cond)):
				cond = K.expand_dims(cond, 1)
			if self.center:
				beta = self.beta_dense(cond) + self.beta
			if self.scale:
				gamma = self.gamma_dense(cond) + self.gamma
		else:
			if self.center:
				beta = self.beta
			if self.scale:
				gamma = self.gamma

		outputs = inputs
		if self.center:
			mean = K.mean(outputs, axis=-1, keepdims=True)
			outputs = outputs - mean
		if self.scale:
			variance = K.mean(K.square(outputs), axis=-1, keepdims=True)
			std = K.sqrt(variance + self.epsilon)
			outputs = outputs / std
			outputs = outputs * gamma
		if self.center:
			outputs = outputs + beta

		return outputs

	def compute_output_shape(self, input_shape):
		if self.conditional:
			return input_shape[0]
		else:
			return input_shape

	def get_config(self):
		config = {
			'center': self.center,
			'scale': self.scale,
			'epsilon': self.epsilon,
			'conditional': self.conditional,
			'hidden_units': self.hidden_units,
			'hidden_activation': activations.serialize(self.hidden_activation),
			'hidden_initializer':
				initializers.serialize(self.hidden_initializer),
		}
		base_config = super(LayerNormalization, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class PositionEmbedding(Layer):
	"""定义位置Embedding，这里的Embedding是可训练的。
	"""
	def __init__(
		self,
		input_dim,
		output_dim,
		merge_mode='add',
		embeddings_initializer='zeros',
		custom_position_ids=False,
		**kwargs
	):
		super(PositionEmbedding, self).__init__(**kwargs)
		self.input_dim = input_dim
		self.output_dim = output_dim
		self.merge_mode = merge_mode
		self.embeddings_initializer = initializers.get(embeddings_initializer)
		self.custom_position_ids = custom_position_ids

	def build(self, input_shape):
		super(PositionEmbedding, self).build(input_shape)
		self.embeddings = self.add_weight(
			name='embeddings',
			shape=(self.input_dim, self.output_dim),
			initializer=self.embeddings_initializer
		)

	def call(self, inputs):
		"""如果custom_position_ids，那么第二个输入为自定义的位置id
		"""
		if self.custom_position_ids:
			inputs, position_ids = inputs
			if K.dtype(position_ids) != 'int32':
				position_ids = K.cast(position_ids, 'int32')
			pos_embeddings = K.gather(self.embeddings, position_ids)
		else:
			input_shape = K.shape(inputs)
			batch_size, seq_len = input_shape[0], input_shape[1]
			pos_embeddings = self.embeddings[:seq_len]
			pos_embeddings = K.expand_dims(pos_embeddings, 0)
			if self.merge_mode != 'add':
				pos_embeddings = K.tile(pos_embeddings, [batch_size, 1, 1])

		if self.merge_mode == 'add':
			return inputs + pos_embeddings
		else:
			return K.concatenate([inputs, pos_embeddings])

	def compute_output_shape(self, input_shape):
		if self.custom_position_ids:
			input_shape = input_shape[0]

		if self.merge_mode == 'add':
			return input_shape
		else:
			return input_shape[:2] + (input_shape[2] + self.output_dim,)

	def get_config(self):
		config = {
			'input_dim': self.input_dim,
			'output_dim': self.output_dim,
			'merge_mode': self.merge_mode,
			'embeddings_initializer':
				initializers.serialize(self.embeddings_initializer),
			'custom_position_ids': self.custom_position_ids,
		}
		base_config = super(PositionEmbedding, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class RelativePositionEmbedding(Layer):
	"""相对位置编码
	来自论文：https://arxiv.org/abs/1803.02155
	"""
	def __init__(
		self, input_dim, output_dim, embeddings_initializer='zeros', **kwargs
	):
		super(RelativePositionEmbedding, self).__init__(**kwargs)
		self.input_dim = input_dim
		self.output_dim = output_dim
		self.embeddings_initializer = initializers.get(embeddings_initializer)

	def build(self, input_shape):
		super(RelativePositionEmbedding, self).build(input_shape)
		self.embeddings = self.add_weight(
			name='embeddings',
			shape=(self.input_dim, self.output_dim),
			initializer=self.embeddings_initializer,
		)

	def call(self, inputs):
		pos_ids = self.compute_position_ids(inputs)
		return K.gather(self.embeddings, pos_ids)

	def compute_position_ids(self, inputs):
		q, v = inputs
		# 计算位置差
		q_idxs = K.arange(0, K.shape(q)[1], dtype='int32')
		q_idxs = K.expand_dims(q_idxs, 1)
		v_idxs = K.arange(0, K.shape(v)[1], dtype='int32')
		v_idxs = K.expand_dims(v_idxs, 0)
		pos_ids = v_idxs - q_idxs
		# 后处理操作
		max_position = (self.input_dim - 1) // 2
		pos_ids = K.clip(pos_ids, -max_position, max_position)
		pos_ids = pos_ids + max_position
		return pos_ids

	def compute_output_shape(self, input_shape):
		return (None, None, self.output_dim)

	def compute_mask(self, inputs, mask):
		return mask[0]

	def get_config(self):
		config = {
			'input_dim': self.input_dim,
			'output_dim': self.output_dim,
			'embeddings_initializer':
				initializers.serialize(self.embeddings_initializer),
		}
		base_config = super(RelativePositionEmbedding, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class RelativePositionEmbeddingT5(RelativePositionEmbedding):
	"""Google T5的相对位置编码
	来自论文：https://arxiv.org/abs/1910.10683
	"""
	def __init__(
		self,
		input_dim,
		output_dim,
		max_distance=128,
		bidirectional=True,
		embeddings_initializer='zeros',
		**kwargs
	):
		super(RelativePositionEmbeddingT5,
			  self).__init__(input_dim, output_dim, **kwargs)
		self.max_distance = max_distance
		self.bidirectional = bidirectional

	def compute_position_ids(self, inputs):
		"""T5的相对位置分桶（直接翻译自官方T5源码）
		"""
		q, v = inputs
		# 计算位置差
		q_idxs = K.arange(0, K.shape(q)[1], dtype='int32')
		q_idxs = K.expand_dims(q_idxs, 1)
		v_idxs = K.arange(0, K.shape(v)[1], dtype='int32')
		v_idxs = K.expand_dims(v_idxs, 0)
		pos_ids = v_idxs - q_idxs
		# 后处理操作
		num_buckets, max_distance = self.input_dim, self.max_distance
		ret = 0
		n = -pos_ids
		if self.bidirectional:
			num_buckets //= 2
			ret += K.cast(K.less(n, 0), 'int32') * num_buckets
			n = K.abs(n)
		else:
			n = K.maximum(n, 0)
		# now n is in the range [0, inf)
		max_exact = num_buckets // 2
		is_small = K.less(n, max_exact)
		val_if_large = max_exact + K.cast(
			K.log(K.cast(n, K.floatx()) / max_exact) /
			np.log(max_distance / max_exact) * (num_buckets - max_exact),
			'int32',
		)
		val_if_large = K.minimum(val_if_large, num_buckets - 1)
		ret += K.switch(is_small, n, val_if_large)
		return ret

	def get_config(self):
		config = {
			'max_distance': self.max_distance,
			'bidirectional': self.bidirectional,
		}
		base_config = super(RelativePositionEmbeddingT5, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class FeedForward(Layer):
	"""FeedForward层，其实就是两个Dense层的叠加
	"""
	def __init__(
		self,
		units,
		activation='relu',
		use_bias=True,
		kernel_initializer='glorot_uniform',
		**kwargs
	):
		super(FeedForward, self).__init__(**kwargs)
		self.units = units
		self.activation = activations.get(activation)
		self.use_bias = use_bias
		self.kernel_initializer = initializers.get(kernel_initializer)

	@integerize_shape
	def build(self, input_shape):
		super(FeedForward, self).build(input_shape)
		output_dim = input_shape[-1]

		self.dense_1 = Dense(
			units=self.units,
			activation=self.activation,
			use_bias=self.use_bias,
			kernel_initializer=self.kernel_initializer
		)
		self.dense_2 = Dense(
			units=output_dim,
			use_bias=self.use_bias,
			kernel_initializer=self.kernel_initializer
		)

	@recompute_grad
	def call(self, inputs):
		x = inputs
		x = self.dense_1(x)
		x = self.dense_2(x)
		return x

	def get_config(self):
		config = {
			'units': self.units,
			'activation': activations.serialize(self.activation),
			'use_bias': self.use_bias,
			'kernel_initializer':
				initializers.serialize(self.kernel_initializer),
		}
		base_config = super(FeedForward, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class ConditionalRandomField(Layer):
	"""纯Keras实现CRF层
	CRF层本质上是一个带训练参数的loss计算层。
	"""
	def __init__(self, lr_multiplier=1, **kwargs):
		super(ConditionalRandomField, self).__init__(**kwargs)
		self.lr_multiplier = lr_multiplier  # 当前层学习率的放大倍数

	@integerize_shape
	def build(self, input_shape):
		super(ConditionalRandomField, self).build(input_shape)
		output_dim = input_shape[-1]
		self._trans = self.add_weight(
			name='trans',
			shape=(output_dim, output_dim),
			initializer='glorot_uniform',
			trainable=True
		)
		if self.lr_multiplier != 1:
			K.set_value(self._trans, K.eval(self._trans) / self.lr_multiplier)

	@property
	def trans(self):
		if self.lr_multiplier != 1:
			return self.lr_multiplier * self._trans
		else:
			return self._trans

	def compute_mask(self, inputs, mask=None):
		return None

	def call(self, inputs, mask=None):
		if mask is not None:
			mask = K.cast(mask, K.floatx())

		return sequence_masking(inputs, mask, 1, 1)

	def target_score(self, y_true, y_pred):
		"""计算目标路径的相对概率（还没有归一化）
		要点：逐标签得分，加上转移概率得分。
		"""
		point_score = tf.einsum('bni,bni->b', y_true, y_pred)  # 逐标签得分
		trans_score = tf.einsum(
			'bni,ij,bnj->b', y_true[:, :-1], self.trans, y_true[:, 1:]
		)  # 标签转移得分
		return point_score + trans_score

	def log_norm_step(self, inputs, states):
		"""递归计算归一化因子
		要点：1、递归计算；2、用logsumexp避免溢出。
		"""
		inputs, mask = inputs[:, :-1], inputs[:, -1:]
		states = K.expand_dims(states[0], 2)  # (batch_size, output_dim, 1)
		trans = K.expand_dims(self.trans, 0)  # (1, output_dim, output_dim)
		outputs = tf.reduce_logsumexp(
			states + trans, 1
		)  # (batch_size, output_dim)
		outputs = outputs + inputs
		outputs = mask * outputs + (1 - mask) * states[:, :, 0]
		return outputs, [outputs]

	def dense_loss(self, y_true, y_pred):
		"""y_true需要是one hot形式
		"""
		# 导出mask并转换数据类型
		mask = K.all(K.greater(y_pred, -1e6), axis=2, keepdims=True)
		mask = K.cast(mask, K.floatx())
		# 计算目标分数
		y_true, y_pred = y_true * mask, y_pred * mask
		target_score = self.target_score(y_true, y_pred)
		# 递归计算log Z
		init_states = [y_pred[:, 0]]
		y_pred = K.concatenate([y_pred, mask], axis=2)
		input_length = K.int_shape(y_pred[:, 1:])[1]
		log_norm, _, _ = K.rnn(
			self.log_norm_step,
			y_pred[:, 1:],
			init_states,
			input_length=input_length
		)  # 最后一步的log Z向量
		log_norm = tf.reduce_logsumexp(log_norm, 1)  # logsumexp得标量
		# 计算损失 -log p
		return log_norm - target_score

	def sparse_loss(self, y_true, y_pred):
		"""y_true需要是整数形式（非one hot）
		"""
		# y_true需要重新明确一下shape和dtype
		y_true = K.reshape(y_true, K.shape(y_pred)[:-1])
		y_true = K.cast(y_true, 'int32')
		# 转为one hot
		y_true = K.one_hot(y_true, K.shape(self.trans)[0])
		return self.dense_loss(y_true, y_pred)

	def dense_accuracy(self, y_true, y_pred):
		"""训练过程中显示逐帧准确率的函数，排除了mask的影响
		此处y_true需要是one hot形式
		"""
		y_true = K.argmax(y_true, 2)
		return self.sparse_accuracy(y_true, y_pred)

	def sparse_accuracy(self, y_true, y_pred):
		"""训练过程中显示逐帧准确率的函数，排除了mask的影响
		此处y_true需要是整数形式（非one hot）
		"""
		# 导出mask并转换数据类型
		mask = K.all(K.greater(y_pred, -1e6), axis=2)
		mask = K.cast(mask, K.floatx())
		# y_true需要重新明确一下shape和dtype
		y_true = K.reshape(y_true, K.shape(y_pred)[:-1])
		y_true = K.cast(y_true, 'int32')
		# 逐标签取最大来粗略评测训练效果
		y_pred = K.cast(K.argmax(y_pred, 2), 'int32')
		isequal = K.cast(K.equal(y_true, y_pred), K.floatx())
		return K.sum(isequal * mask) / K.sum(mask)

	def get_config(self):
		config = {
			'lr_multiplier': self.lr_multiplier,
		}
		base_config = super(ConditionalRandomField, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class MaximumEntropyMarkovModel(Layer):
	"""（双向）最大熵隐马尔可夫模型
	作用和用法都类似CRF，但是比CRF更快更简单。
	"""
	def __init__(self, lr_multiplier=1, hidden_dim=None, **kwargs):
		super(MaximumEntropyMarkovModel, self).__init__(**kwargs)
		self.lr_multiplier = lr_multiplier  # 当前层学习率的放大倍数
		self.hidden_dim = hidden_dim  # 如果非None，则将转移矩阵低秩分解

	@integerize_shape
	def build(self, input_shape):
		super(MaximumEntropyMarkovModel, self).build(input_shape)
		output_dim = input_shape[-1]

		if self.hidden_dim is None:
			self._trans = self.add_weight(
				name='trans',
				shape=(output_dim, output_dim),
				initializer='glorot_uniform',
				trainable=True
			)
			if self.lr_multiplier != 1:
				K.set_value(
					self._trans,
					K.eval(self._trans) / self.lr_multiplier
				)
		else:
			self._l_trans = self.add_weight(
				name='l_trans',
				shape=(output_dim, self.hidden_dim),
				initializer='glorot_uniform',
				trainable=True
			)
			self._r_trans = self.add_weight(
				name='r_trans',
				shape=(output_dim, self.hidden_dim),
				initializer='glorot_uniform',
				trainable=True
			)

			if self.lr_multiplier != 1:
				K.set_value(
					self._l_trans,
					K.eval(self._l_trans) / self.lr_multiplier
				)
				K.set_value(
					self._r_trans,
					K.eval(self._r_trans) / self.lr_multiplier
				)

	@property
	def trans(self):
		if self.lr_multiplier != 1:
			return self.lr_multiplier * self._trans
		else:
			return self._trans

	@property
	def l_trans(self):
		if self.lr_multiplier != 1:
			return self.lr_multiplier * self._l_trans
		else:
			return self._l_trans

	@property
	def r_trans(self):
		if self.lr_multiplier != 1:
			return self.lr_multiplier * self._r_trans
		else:
			return self._r_trans

	def compute_mask(self, inputs, mask=None):
		return None

	def call(self, inputs, mask=None):
		if mask is not None:
			mask = K.cast(mask, K.floatx())

		return sequence_masking(inputs, mask, 1, 1)

	def reverse_sequence(self, inputs, mask=None):
		if mask is None:
			return [x[:, ::-1] for x in inputs]
		else:
			length = K.cast(K.sum(mask, 1), 'int32')
			return [tf.reverse_sequence(x, length, seq_axis=1) for x in inputs]

	def basic_loss(self, y_true, y_pred, go_backwards=False):
		"""y_true需要是整数形式（非one hot）
		"""
		# 导出mask并转换数据类型
		mask = K.all(K.greater(y_pred, -1e6), axis=2)
		mask = K.cast(mask, K.floatx())
		# y_true需要重新明确一下shape和dtype
		y_true = K.reshape(y_true, K.shape(y_pred)[:-1])
		y_true = K.cast(y_true, 'int32')
		# 反转相关
		if self.hidden_dim is None:
			if go_backwards:  # 是否反转序列
				y_true, y_pred = self.reverse_sequence([y_true, y_pred], mask)
				trans = K.transpose(self.trans)
			else:
				trans = self.trans
			histoty = K.gather(trans, y_true)
		else:
			if go_backwards:  # 是否反转序列
				y_true, y_pred = self.reverse_sequence([y_true, y_pred], mask)
				r_trans, l_trans = self.l_trans, self.r_trans
			else:
				l_trans, r_trans = self.l_trans, self.r_trans
			histoty = K.gather(l_trans, y_true)
			histoty = tf.einsum('bnd,kd->bnk', histoty, r_trans)
		# 计算loss
		histoty = K.concatenate([y_pred[:, :1], histoty[:, :-1]], 1)
		y_pred = (y_pred + histoty) / 2
		loss = K.sparse_categorical_crossentropy(
			y_true, y_pred, from_logits=True
		)
		return K.sum(loss * mask) / K.sum(mask)

	def sparse_loss(self, y_true, y_pred):
		"""y_true需要是整数形式（非one hot）
		"""
		loss = self.basic_loss(y_true, y_pred, False)
		loss = loss + self.basic_loss(y_true, y_pred, True)
		return loss / 2

	def dense_loss(self, y_true, y_pred):
		"""y_true需要是one hot形式
		"""
		y_true = K.argmax(y_true, 2)
		return self.sparse_loss(y_true, y_pred)

	def basic_accuracy(self, y_true, y_pred, go_backwards=False):
		"""训练过程中显示逐帧准确率的函数，排除了mask的影响
		此处y_true需要是整数形式（非one hot）
		"""
		# 导出mask并转换数据类型
		mask = K.all(K.greater(y_pred, -1e6), axis=2)
		mask = K.cast(mask, K.floatx())
		# y_true需要重新明确一下shape和dtype
		y_true = K.reshape(y_true, K.shape(y_pred)[:-1])
		y_true = K.cast(y_true, 'int32')
		# 反转相关
		if self.hidden_dim is None:
			if go_backwards:  # 是否反转序列
				y_true, y_pred = self.reverse_sequence([y_true, y_pred], mask)
				trans = K.transpose(self.trans)
			else:
				trans = self.trans
			histoty = K.gather(trans, y_true)
		else:
			if go_backwards:  # 是否反转序列
				y_true, y_pred = self.reverse_sequence([y_true, y_pred], mask)
				r_trans, l_trans = self.l_trans, self.r_trans
			else:
				l_trans, r_trans = self.l_trans, self.r_trans
			histoty = K.gather(l_trans, y_true)
			histoty = tf.einsum('bnd,kd->bnk', histoty, r_trans)
		# 计算逐标签accuracy
		histoty = K.concatenate([y_pred[:, :1], histoty[:, :-1]], 1)
		y_pred = (y_pred + histoty) / 2
		y_pred = K.cast(K.argmax(y_pred, 2), 'int32')
		isequal = K.cast(K.equal(y_true, y_pred), K.floatx())
		return K.sum(isequal * mask) / K.sum(mask)

	def sparse_accuracy(self, y_true, y_pred):
		"""训练过程中显示逐帧准确率的函数，排除了mask的影响
		此处y_true需要是整数形式（非one hot）
		"""
		accuracy = self.basic_accuracy(y_true, y_pred, False)
		accuracy = accuracy + self.basic_accuracy(y_true, y_pred, True)
		return accuracy / 2

	def dense_accuracy(self, y_true, y_pred):
		"""训练过程中显示逐帧准确率的函数，排除了mask的影响
		此处y_true需要是one hot形式
		"""
		y_true = K.argmax(y_true, 2)
		return self.sparse_accuracy(y_true, y_pred)

	def get_config(self):
		config = {
			'lr_multiplier': self.lr_multiplier,
			'hidden_dim': self.hidden_dim,
		}
		base_config = super(MaximumEntropyMarkovModel, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


class Loss(Layer):
	"""特殊的层，用来定义复杂loss
	"""
	def __init__(self, output_axis=None, **kwargs):
		super(Loss, self).__init__(**kwargs)
		self.output_axis = output_axis

	def call(self, inputs, mask=None):
		loss = self.compute_loss(inputs, mask)
		self.add_loss(loss)
		if self.output_axis is None:
			return inputs
		elif isinstance(self.output_axis, list):
			return [inputs[i] for i in self.output_axis]
		else:
			return inputs[self.output_axis]

	def compute_loss(self, inputs, mask=None):
		raise NotImplementedError

	def compute_output_shape(self, input_shape):
		if self.output_axis is None:
			return input_shape
		elif isinstance(self.output_axis, list):
			return [input_shape[i] for i in self.output_axis]
		else:
			return input_shape[self.output_axis]

	def get_config(self):
		config = {
			'output_axis': self.output_axis,
		}
		base_config = super(Loss, self).get_config()
		return dict(list(base_config.items()) + list(config.items()))


custom_objects = {
	'Embedding': Embedding,
	'BiasAdd': BiasAdd,
	'MultiHeadAttention': MultiHeadAttention,
	'LayerNormalization': LayerNormalization,
	'PositionEmbedding': PositionEmbedding,
	'RelativePositionEmbedding': RelativePositionEmbedding,
	'RelativePositionEmbeddingT5': RelativePositionEmbeddingT5,
	'FeedForward': FeedForward,
	'ConditionalRandomField': ConditionalRandomField,
	'MaximumEntropyMarkovModel': MaximumEntropyMarkovModel,
	'Loss': Loss,
}

keras.utils.get_custom_objects().update(custom_objects)
