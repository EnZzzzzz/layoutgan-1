""" 添加attention模块 """
import os
import random

import numpy as np 
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.nn.functional as F
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
from tensorboardX import SummaryWriter

class Attention(nn.Module):
    def __init__(self, in_channels, out_channels=None, dimension=1, sub_sample=False, bn=True, generate=True):
        super(Attention, self).__init__()
        if out_channels is None:
            self.out_channels = in_channels//2 if in_channels>1 else 1
        self.out_channels = out_channels
        self.generate = generate
        self.g = nn.Conv1d(in_channels, self.out_channels, kernel_size=1, stride=1, padding=0) #U
        self.W = nn.Sequential(nn.Conv1d(self.out_channels, in_channels, kernel_size=1, stride=1, padding=0),
                                 nn.BatchNorm1d(in_channels))
        nn.init.constant(self.W[1].weight, 0)
        nn.init.constant(self.W[1].bias, 0)

        self.theta = nn.Conv1d(in_channels, self.out_channels, kernel_size=1, stride=1, padding=0)
        self.phi = nn.Conv1d(in_channels, self.out_channels, kernel_size=1, stride=1, padding=0)

        if sub_sample: #是否需要下采样，这里会用到最大池化
            self.g=nn.Sequential(self.g, nn.MaxPool1d)
            self.phi=nn.Sequential(self.phi, nn.MaxPool1d)

    def forward(self, x):
        batch_size = x.size(0) #批次大小
        g_x = self.g(x).view(batch_size, self.out_channels, -1)
        g_x = g_x.permute(0, 2, 1)

        theta_x = self.theta(x).view(batch_size, self.out_channels, -1)  
        theta_x = theta_x.permute(0, 2, 1)
        phi_x = self.phi(x).view(batch_size, self.out_channels, -1)
        f = torch.matmul(theta_x, phi_x) #计算H 
 
        N = f.size(-1)
        f_div_c = f/N
        y = torch.matmul(f_div_c, g_x)
        y = y.permute(0,2,1).contiguous()
        y = y.view(batch_size, self.out_channels, *x.size()[2:])
        W_y = self.W(y)
        if self.generate: 
            output = W_y + x
        else:
            output=W_y
        return output

class Generator(nn.Module):
    def __init__(self, num_elements, geo_num, cls_num): #位置个数，元素个数
        super(Generator, self).__init__()
        self.geo_num = geo_num
        self.cls_num = cls_num
        self.feature_size = geo_num + cls_num

        #Encoder
        self.encoder_fc1 = nn.Linear(self.feature_size, self.feature_size*2)
        self.encoder_bn1 = nn.BatchNorm1d(num_elements)  
        self.encoder_fc2 = nn.Linear(self.feature_size*2, self.feature_size*2*2)
        self.encoder_bn2 = nn.BatchNorm1d(num_elements)
        self.encoder_fc3 = nn.Linear(self.feature_size*2*2, self.feature_size*2*2)

        #stacked relation 
        self.attention_1 = Attention(num_elements, 1)
        self.attention_2 = Attention(num_elements, 1)
        self.attention_3 = Attention(num_elements, 1)
        self.attention_4 = Attention(num_elements, 1)

        #Decoder
        self.decoder_fc4 = nn.Linear(self.feature_size*2*2, self.feature_size*2)
        self.decoder_bn4 = nn.BatchNorm1d(num_elements)
        self.decoder_fc5 = nn.Linear(self.feature_size*2, self.feature_size)

        #branch
        self.fc6 = nn.Linear(self.feature_size, cls_num)
        self.fc7 = nn.Linear(self.feature_size, geo_num)

    def forward(self, x):

        x = torch.relu(self.encoder_bn1(self.encoder_fc1(x)))
        x = torch.relu(self.encoder_bn2(self.encoder_fc2(x)))
        x = torch.sigmoid(self.encoder_fc3(x))


        x = self.attention_1(x)
        x = self.attention_2(x)
        x = self.attention_3(x)
        x = self.attention_4(x)

        out = torch.relu(self.decoder_bn4(self.decoder_fc4(x)))
        out = torch.relu(self.decoder_fc5(out))

        
        cls = torch.sigmoid(self.fc6(out))
        #cls = torch.nn.LeakyReLU(self.fc6(out))
        #cls = torch.relu(self.fc6(out))
        geo = torch.sigmoid(self.fc7(out))
        #geo = torch.nn.LeakyReLU(self.fc7(out))
        #geo = torch.relu(self.fc7(out))
        output = torch.cat((cls, geo), 2)
        return output

#判别器
class Discriminator(nn.Module):
    def __init__(self, batch_size):
        super(Discriminator, self).__init__()

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.batch_size = batch_size

        # Convolution Layers
        self.conv1 = nn.Conv2d(1, 4, 3, 1, 1)
        torch.nn.init.normal_(self.conv1.weight, 0, 0.02)
        self.conv1_bn = nn.BatchNorm2d(4)
        self.conv2 = nn.Conv2d(4, 8, 3, 1, 1)
        torch.nn.init.normal_(self.conv2.weight, 0, 0.02)
        self.conv2_bn = nn.BatchNorm2d(8)
        self.conv3 = nn.Conv2d(8, 16, 3, 1, 1)
        torch.nn.init.normal_(self.conv3.weight, 0, 0.02)
        self.conv3_bn = nn.BatchNorm2d(16)

        # Fully Connected Layers
        self.fc1 = nn.Linear(16*7*7, 128)
        torch.nn.init.normal_(self.fc1.weight, 0, 0.02)
        self.fc2 = nn.Linear(128, 1)
        torch.nn.init.normal_(self.fc2.weight, 0, 0.02)


    def forward(self, x_in):
        # Passing through wireframe rendering
        x_wf = self.wireframe_rendering(x_in)

        # Passing through conv layers
        x = torch.nn.functional.max_pool2d(F.relu(self.conv1_bn(self.conv1(x_wf))), 2, 2)
        x = torch.nn.functional.max_pool2d(F.relu(self.conv2_bn(self.conv2(x))), 2, 2)
        x = torch.relu(self.conv3_bn(self.conv3(x)))

        # Flattening and passing through FC Layers
        x = x.view(x.size(0), -1)
        x = torch.relu(self.fc1(x))
        x = self.fc2(x)
        x = x.mean(0)

        return x.view(1)

    def wireframe_rendering(self, x_in):
        """ 线框渲染 """
        def k(x):
            return torch.relu(1-torch.abs(x))

        w = 28
        h = 28

        p = x_in[:, :, 0]
        theta = x_in[:, :, 1:]

        batch_size, num_elements, geo_size = theta.shape

        theta[:, :, 0] *= w
        theta[:, :, 1] *= h

        assert(p.shape[0] == batch_size and p.shape[1] == num_elements)

        x = np.repeat(np.arange(w), h).reshape(w,h)
        y = np.transpose(x)

        x_tensor = torch.from_numpy(x)
        y_tensor = torch.from_numpy(y)
        x_tensor = x_tensor.view(1, w, h)
        y_tensor = y_tensor.view(1, w, h)

        base_tensor = torch.cat([x_tensor, y_tensor]).type(torch.FloatTensor).to(self.device)
        base_tensor = base_tensor.repeat(batch_size*num_elements, 1, 1, 1)
        theta= theta.view(batch_size*num_elements, geo_size, 1, 1)
        p = p.view(batch_size, num_elements, 1, 1)

        F = k(base_tensor[:,0,:,:] - theta[:,0]) * k(base_tensor[:, 1, :, :] - theta[:, 1])
        F = F.view(batch_size, num_elements, w, h)

        p_times_F = p * F

        I = torch.max(p_times_F, dim=1)[0]

        I = I.view(batch_size, 1, w, h)
        return I
