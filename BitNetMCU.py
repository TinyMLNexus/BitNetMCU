import torch
import torch.nn as nn
import torch.nn.init as init
import numpy as np
import torch.nn.functional as F

# @cpldcpu 2024-March-24

class FCMNIST(nn.Module):
    """
    Fully Connected Neural Network for MNIST dataset.
    16x16 input image, 3 hidden layers with a configurable width.

    @cpldcpu 2024-March-24

    """
    def __init__(self,network_width1=64,network_width2=64,network_width3=64,QuantType='Binary',WScale='PerTensor',NormType='RMS', quantscale=0.25):
        super(FCMNIST, self).__init__()

        self.network_width1 = network_width1
        self.network_width2 = network_width2
        self.network_width3 = network_width3
        self.quantscale = quantscale

        self.fc1 = BitLinear(1* 1 *16 *16, network_width1,QuantType=QuantType,NormType=NormType, WScale=WScale, quantscale=quantscale, prunable=True)
        self.fc2 = BitLinear(network_width1, network_width2,QuantType=QuantType,NormType=NormType, WScale=WScale , quantscale=quantscale, prunable=True )
        if network_width3>0:
            self.fc3 = BitLinear(network_width2, network_width3,QuantType=QuantType,NormType=NormType, WScale=WScale , quantscale=quantscale, prunable=False)
            self.fcl = BitLinear(network_width3, 10,QuantType=QuantType,NormType=NormType, WScale=WScale , quantscale=quantscale, prunable=False)
        else:
            self.fcl = BitLinear(network_width2, 10,QuantType=QuantType,NormType=NormType, WScale=WScale , quantscale=quantscale, prunable=False)
            
        # self.dropout = nn.Dropout(0.10)

    def forward(self, x):
        x = x.view(x.size(0), -1)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        if self.network_width3>0:
            x = F.relu(self.fc3(x))
        # x = self.dropout(x)
        x = self.fcl(x)
        return x
    
class BitLinear(nn.Linear):
    """
    Linear convolution layer with quantization aware training and normalization.
    Configurable quantization and normalization types.

    Quantization Types:
    - Binary         : 1 bit
    - Ternary        : 1.58 bits
    - BinaryBalanced : 1 bit, weights are balanced around zero
    - 2bitsym        : 2 bit symmetric
    - 4bitsym        : 4 bit symmetric
    - FP130          : 4 bit shift encoding
    - 8bit           : 8 bit

    Normalization Types:
    - RMS            : Root Mean Square
    - Lin            : L1 Norm
    - BatchNorm      : Batch Normalization

    WScale 
    - PerTensor      : The weight scaling is calculated per Tensor
    - PerOutput      : The weight scaling is calculated per Output

    quantcale
    - scalar         : The scale factor for the weight quantization, the default of 0.25 
                       biases the stddev of the weights toward 25% of the maximum scale

    Implementation based on:
    https://github.com/microsoft/unilm/blob/master/bitnet/The-Era-of-1-bit-LLMs__Training_Tips_Code_FAQ.pdf
    
    This is not optimized for speed or efficiency...

    @cpldcpu 2024-March-24
    """
    def __init__(self, in_features, out_features, bias=False, QuantType='Binary', WScale='PerTensor', NormType='RMS', quantscale=0.25, prunable=False):
        super(BitLinear, self).__init__(in_features, out_features, bias=False)
        self.QuantType = QuantType
        self.NormType = NormType
        self.WScale = WScale
        self.quantscale = quantscale
        self.prunable = prunable

        # flat init - does not help so keep default
        # fan_in, _ = init._calculate_fan_in_and_fan_out(self.weight)
        # print(fan_in)
        # init.uniform_(self.weight, -np.sqrt(2/fan_in), np.sqrt(2/fan_in))

        self.mask = nn.Parameter(torch.ones_like(self.weight), requires_grad=False)

    def forward(self, x):
        """
        Args:
        x: an input tensor with shape [n, d]
        Returns:
        y: an output tensor with shape [n, k]
        """
        w = self.weight * self.mask # a weight tensor with shape [d, k]
        x_norm = self.Normalize(x)

        if self.QuantType == 'None':
            y = F.linear(x_norm, w)
        else:
            # A trick for implementing Straight-Through-Estimator (STE) using detach()
            x_quant = x_norm + (self.activation_quant(x_norm) - x_norm).detach()
            w_quant = w + (self.weight_quant(w) - w).detach()
            y = F.linear(x_quant, w_quant )
        return y
    
    def prune(self, percent=25):
        """
        Update the mask and weights to prune the weights with the lowest absolute values.
        Args:
        percent: the percentage of weights to prune
        """
        abs_weights = self.weight.abs()
        flat_abs_weights = abs_weights.view(-1)
        threshold = torch.kthvalue(flat_abs_weights, int(flat_abs_weights.shape[0] * percent / 100))[0]
        print(f'Threshold:{threshold}')
        mask = abs_weights > threshold
        # print(mask.float())
        self.weight.data.mul_(mask.float())
        self.mask = torch.nn.Parameter(mask.float(), requires_grad=False)

    def activation_quant(self, x):
        """ Per-token quantization to 8 bits. No grouping is needed for quantization.
        Args:
        x: an activation tensor with shape [n, d]
        Returns:
        y: a quantized activation tensor with shape [n, d]
        """
        scale = 127.0 / x.abs().max(dim=-1, keepdim=True).values.clamp_(min=1e-5)
        y = (x * scale).round().clamp_(-128, 127) / scale
        return y

    def weight_quant(self, w):
        """ Per-tensor quantization.
        Args:
        w: a weight tensor with shape [d, k]
        Returns:
        u: a quantized weight with shape [d, k]
        """
        if self.WScale=='PerOutput':
            mag = w.abs().max(dim=-1, keepdim=True)[0].clamp_(min=1e-5)
            magmax = mag.max()
            scalequant = (127.0*mag/magmax).round().clamp_(1,127)
            # mag = scalequant * magmax / 127.0
        elif self.WScale=='PerTensor':
            mag = w.abs().mean().clamp_(min=1e-5)
        else:
            raise AssertionError(f"Invalid WScale: {self.WScale}. Expected one of: 'PerTensor', 'PerOutput'")

        if self.QuantType == 'Ternary': # 1.58bits
            scale = 1.0 / mag
            u = (w * scale).round().clamp_(-1, 1) / scale
        elif self.QuantType == 'Binary': # 1 bit
            scale = mag
            e = w.mean()
            u = (w - e).sign() * scale
        elif self.QuantType == 'BinarySym': # 1 bit
            scale = mag
            # e = w.mean()
            u = w.sign() * scale
        elif self.QuantType == '2bitsym':
            scale = 1.0 / mag # 2 worst, 1 better, 1.5 almost as bad as 2
            u = ((w * scale - 0.5).round().clamp_(-2, 1) + 0.5) / scale
        elif self.QuantType == '4bit': # 4 bit in one-complement encoding for inference with multiplication
            scale = self.quantscale * 8.0 / mag # 2.0 for tensor, 6.5 for output
            u = ((w * scale).round().clamp_(-8, 7)) / scale        
        elif self.QuantType == '4bitsym':
            scale = self.quantscale * 8.0 / mag # 2.0 for tensor, 6.5 for output
            u = ((w * scale - 0.5).round().clamp_(-8, 7) + 0.5) / scale        
        elif self.QuantType ==  'FP130': # encoding (F1.3.0) : S * ( 2^E3 + 1) -> min 2^0 = 1, max 2^7 = 128
            scale = 128.0 * self.quantscale / mag
            e = ((w * scale).abs()).log2().floor().clamp_(0, 7)
            u = w.sign()*(e.exp2()) / scale            
        elif self.QuantType == '5bitsym':
            scale = 16.0 * self.quantscale / mag # 4.0 for tensor, 13 for output
            u = ((w * scale - 0.5).round().clamp_(-16, 15) + 0.5) / scale        
        elif self.QuantType == '8bit': # -128 to 127
            scale = 128.0 * self.quantscale / mag
            u = (w * scale).round().clamp_(-128, 127) / scale   
        else:
            raise AssertionError(f"Invalid QuantType: {self.QuantType}. Expected one of: 'Binary', 'BinaryBalanced', '2bitsym', '4bitsym', '8bit'")
        
        return u

    def Normalize(self, x):
        """ Normalization. Normalizes along the last dimension -> different normalization value for each activation vector.
        Args:
        x: an input tensor with shape [n, d]
        Returns:
        y: a normalized tensor with shape [n, d]
        """
        if self.NormType == 'RMS':
            y = torch.sqrt(torch.mean(x**2, dim=-1, keepdim=True))
            z =x / y
        elif self.NormType == 'Lin':
            y = torch.mean(torch.abs(x), dim=-1, keepdim=True)
            z =x / y
        elif self.NormType == 'BatchNorm':
            z = (x - torch.mean(x, dim=-1, keepdim=True)) / (torch.std(x, dim=-1, keepdim=True) + 1e-5)
        else:
            raise AssertionError(f"Invalid NormType: {self.NormType}. Expected one of: 'RMS', 'Lin', 'BatchNorm'")
        return z

class QuantizedModel:
    """
    This class represents a quantized model. It provides functionality to quantize a given model.
    """   
     
    def __init__(self, model = None, force_quantization = None, quantscale=0.25):
        self.quantized_model=None
        self.total_bits=0
        self.force_quantization = force_quantization
        self.quantscale = quantscale

        if model is not None:
            self.quantized_model, _ = self.quantize(model)
            self.quantscale = model.quantscale

    def totalbits(self):
        """
        Returns the total number of bits used by the quantized model.
        """
        return self.total_bits
                                      
    def quantize(self,model):
        """
        This method quantizes the weights of the given model.

        Parameters:
        model (torch.nn.Module): The PyTorch model to be quantized.
        Only the weights of the BitLinear layers are quantized.

        Returns:
        list: A list of dictionaries containing information about each layer of the quantized model.
        int: The total number of bits used by the quantized model.
        """
        quantized_model = []
        totalbits = 0
        for i, layer in enumerate(model.modules()):
            if isinstance(layer, BitLinear):
                if layer.prunable:
                    w         = layer.weight.data * layer.mask
                else:
                    w         = layer.weight.data
                QuantType = layer.QuantType
                
                if self.force_quantization != None:
                    QuantType = self.force_quantization
                                
                if layer.WScale=='PerOutput':
                    # mag = w.abs().mean(dim=-1, keepdim=True).clamp_(min=1e-5)
                    mag = w.abs().max(dim=-1, keepdim=True)[0].clamp_(min=1e-5)

                    magmax = mag.max()
                    scalequant = (127.0*mag/magmax).round().clamp_(1,127)
                    mag = scalequant * magmax / 127.0

                    numscale = len (mag)                
                elif layer.WScale=='PerTensor':
                    mag = w.abs().mean().clamp_(min=1e-5)
                    numscale = 0                    
                else:
                    raise AssertionError(f"Invalid WScale: {self.WScale}. Expected one of: 'PerTensor', 'PerOutput'")

                # print (numscale, mag)

                if QuantType == 'Ternary': # 1.58bits
                    scale = 1.0 / mag
                    u = (w * scale).round().clamp_(-1, 1) 
                    bpw = 1.6 # stuffing 5 ternary weights into 8 bits
                elif QuantType == 'Binary': # 1 bit
                    scale = mag
                    e = w.mean()
                    u = (w - e).sign() 
                    bpw = 1
                elif QuantType == '2bitsym':
                    scale = 1.0 / mag # 2 worst, 1 better, 1.5 almost as bad as 2
                    u = ((w * scale - 0.5).round().clamp_(-2, 1) + 0.5) 
                    bpw = 2
                elif QuantType == '4bit': # 4 bit in one-complement encoding for inference with multiplication
                    scale = 8.0 * self.quantscale / mag # 2.0 for tensor, 6.5 for output
                    u = ((w * scale).round().clamp_(-8, 7)) 
                    bpw = 4
                elif QuantType == '4bitsym':
                    scale = 8.0 * self.quantscale / mag # 2.0 for tensor, 6.5 for output
                    u = ((w * scale - 0.5).round().clamp_(-8, 7) + 0.5) 
                    bpw = 4
                elif QuantType ==  'FP130': 
                    scale = 128.0 * self.quantscale / mag 
                    e = ((w * scale ).abs()).log2().floor().clamp_(0, 7)
                    u = w.sign()*(e.exp2() )    
                    bpw = 4                       
                elif QuantType == '5bitsym':
                    scale = 16.0 * self.quantscale / mag # 4.0 for tensor, 14 for output
                    u = ((w * scale - 0.5).round().clamp_(-16, 15) + 0.5) 
                    bpw = 5
                elif QuantType == '8bit':
                    scale = 128.0 * self.quantscale / mag
                    u = (w * scale).round().clamp_(-128, 127) 
                    bpw = 8
                elif QuantType == 'None':
                    scale = 1.0 / mag
                    u  = w * scale
                    bpw = 32
                else:
                    raise AssertionError(f"Invalid QuantType: {self.QuantType}. Expected one of: 'Binary', 'BinaryBalanced', '2bitsym', '4bitsym', '8bit'")

                totalbits += bpw * u.numel() + numscale * 8
                quantized_weight = u.cpu().numpy()

                layer_info = {
                    'layer_order': i,
                    'incoming_weights': quantized_weight.shape[1],
                    'outgoing_weights': quantized_weight.shape[0],
                    'quantized_weights': quantized_weight.tolist(),
                    'WScale': layer.WScale,
                    'quantized_scale': scalequant.cpu().numpy().tolist() if layer.WScale=='PerOutput' else [],
                    'bpw': bpw, # bits per weight
                    'quantization_type': QuantType
                }
                quantized_model.append(layer_info)

        self.total_bits = totalbits                
        self.quantized_model = quantized_model

        return quantized_model, totalbits 

    def inference_quantized(self, input_data):
        """
        This function performs inference on the given quantized model with the provided input data.

        Parameters:
        quantized_model (list): A list of dictionaries containing information about each layer of the quantized model.
        input_data (torch.Tensor): The input data to be used for inference.

        Returns:
        torch.Tensor: The output of the model after performing inference.
        """
        
        if not self.quantized_model:
            raise ValueError("quantized_model is empty or None")

        scale = 127.0 / np.maximum(np.abs(input_data).max(axis=-1, keepdims=True), 1e-5)
        current_data = np.round(input_data * scale).clip(-128, 127) 

        for layer_info in self.quantized_model[:-1]:  # For all layers except the last one
            weights = np.array(layer_info['quantized_weights']) 
            conv = np.dot(current_data, weights.T)  # Matrix multiplication

            if layer_info['WScale']=='PerOutput':
                scale = np.array(layer_info['quantized_scale']).transpose()
                conv = conv * scale

            max = np.maximum(conv.max(axis=-1, keepdims=True), 1e-5)
            rescale = np.exp2(np.floor(np.log2(127.0 / max))) # Emulate normalization by shift as in C inference engine
            # rescale = 127.0 / np.maximum(conv.max(axis=-1, keepdims=True), 1e-5)   # Normalize to max 1.7 range
            current_data = np.round(conv * rescale).clip(0, 127)  # Quantize the output and ReLU

        # no renormalization for the last layer
        weights = np.array(self.quantized_model[-1]['quantized_weights'])
        logits = np.dot(current_data, weights.T)  # Matrix multiplication

        if self.quantized_model[-1]['WScale']=='PerOutput':
            scale = np.array(self.quantized_model[-1]['quantized_scale']).transpose()
            logits = logits * scale

        return logits
    
    def get_activations(self, input_data):
        """
        This function performs inference on the given quantized model with the provided input data and returns the activations after each layer.

        Parameters:
        input_data (torch.Tensor): The input data to be used for inference.

        Returns:
        list: A list of numpy arrays, each representing the activations after each layer.
        """

        if not self.quantized_model:
            raise ValueError("quantized_model is empty or None")

        scale = 127.0 / np.maximum(np.abs(input_data).max(axis=-1, keepdims=True), 1e-5)
        current_data = np.round(input_data * scale).clip(-128, 127) 

        activations = []  # List to store the activations after each layer

        for layer_info in self.quantized_model[:-1]:  # For all layers except the last one
            weights = np.array(layer_info['quantized_weights']) 
            conv = np.dot(current_data, weights.T)  # Matrix multiplication
            max = np.maximum(conv.max(axis=-1, keepdims=True), 1e-5)
            rescale = np.exp2(np.floor(np.log2(127.0 / max)))
            # rescale = 127.0 / np.maximum(conv.max(axis=-1, keepdims=True), 1e-5)
            current_data = np.round(conv * rescale).clip(0, 127)  # Quantize the output and ReLU

            activations.append(current_data)  # Store the activations after this layer

        # no renormalization for the last layer
        weights = np.array(self.quantized_model[-1]['quantized_weights'])
        logits = np.dot(current_data, weights.T)  # Matrix multiplication

        activations.append(logits)  # Store the activations after the last layer

        return activations