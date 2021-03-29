#!/usr/bin/env python
# coding: utf-8

# In[1]:


import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
import numpy as np
import torchvision
from torchvision import datasets, models, transforms, utils
from torch.utils.data import Dataset
import pandas as pd
import matplotlib.pyplot as plt
import time
import os
import argparse
import copy
from sklearn.model_selection import GroupShuffleSplit, GroupKFold
import datetime
from PIL import Image
import torch.nn.functional as F
from tqdm import tqdm
from scipy.ndimage import center_of_mass
import torch.hub
from sklearn.metrics import mean_squared_error
from itertools import permutations  


# In[2]:


device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

torch.manual_seed(0)
np.random.seed(0)

torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False


# In[3]:


def train_model_snapshot(model, criterion, eval_criterion, lr, dataloaders, dataset_sizes, device, num_cycles, num_epochs_per_cycle):
    since = time.time()

    best_model_wts = copy.deepcopy(model.state_dict())
    best_acc = 0.0
    best_loss = 1000000.0
    model_w_arr = []
    for cycle in range(num_cycles):
        #initialize optimizer and scheduler each cycle
        #optimizer = optim.SGD(model.parameters(), lr=lr, momentum=0.9)
        optimizer = optim.Adam([{"params": model.model_ft.parameters(), "lr": lr*3/time_steps},
                                #{"params": model.fc1.parameters(), "lr": lr},
                                {"params": model.fc.parameters(), "lr": lr}],
                                lr=lr)
        scheduler = lr_scheduler.CosineAnnealingLR(optimizer, num_epochs_per_cycle*len(dataloaders['train']))
        for epoch in range(num_epochs_per_cycle):
            print('Cycle {}: Epoch {}/{}'.format(cycle, epoch, num_epochs_per_cycle - 1))
            print('-' * 10)

            # Each epoch has a training and validation phase
            for phase in ['train', 'val']:
                if phase == 'train':
                    model.train()  # Set model to training mode
                else:
                    model.eval()   # Set model to evaluate mode

                running_loss = 0.0
                running_corrects = 0

                # Iterate over data.
                for inputs, labels in dataloaders[phase]:
                    inputs = inputs.to(device)
                    labels = labels.to(device)                    

                    # zero the parameter gradients
                    optimizer.zero_grad()

                    # forward
                    # track history if only in train
                    with torch.set_grad_enabled(phase == 'train'):
                        outputs = model(inputs)
                        loss = criterion(outputs, labels.reshape(-1,1))
                        eval_loss = eval_criterion(outputs, labels.reshape(-1,1))
                        # backward + optimize only if in training phase
                        if phase == 'train':
                            loss.backward()
                            optimizer.step()
                            scheduler.step()
                    
                    # statistics
                    running_loss += eval_loss.item() * inputs.size(0)

                epoch_loss = np.sqrt(running_loss / dataset_sizes[phase])

                print('{} Loss: {:.4f}'.format(
                    phase, epoch_loss))

                # deep copy the model
                if phase == 'val' and epoch_loss < best_loss:
                    best_loss = epoch_loss
                    best_model_wts = copy.deepcopy(model.state_dict())
            print()
        # deep copy snapshot
        model_w_arr.append(copy.deepcopy(model.state_dict()))

    ensemble_loss = 0.0

    #predict on validation using snapshots
    for inputs, labels in dataloaders['val']:
        inputs = inputs.to(device)
        labels = labels.to(device)

        # forward
        # track history if only in train
        pred = torch.zeros((inputs.shape[0], 1), dtype = torch.float32).to(device)
        for weights in model_w_arr:
            model.load_state_dict(weights)
            model.eval()
            outputs = model(inputs)
            pred += outputs
        
        pred /= num_cycles
        eval_loss = eval_criterion(pred, labels.reshape(-1,1))
        ensemble_loss += eval_loss.item() * inputs.size(0)
    
    ensemble_loss /= dataset_sizes['val']
    ensemble_loss = np.sqrt(ensemble_loss)

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    print('Ensemble Loss : {:4f}, Best val Loss: {:4f}'.format(ensemble_loss, best_loss))
    
    return model_w_arr, ensemble_loss, best_loss


# In[4]:


def test(model, models_w_arr, loader, n_imgs, device):
    res = np.zeros((n_imgs, 1), dtype = np.float32)
    for weights in models_w_arr:
        model.load_state_dict(weights) 
        model.eval()
        res_arr = []
        for inputs, inputs2, _ in loader:
            inputs = inputs.to(device)
            inputs2 = inputs2.to(device)
            # forward
            with torch.set_grad_enabled(False):
                outputs = model(inputs, inputs2)    
                res_arr.append(outputs.detach().cpu().numpy())
        res_arr = np.concatenate(res_arr, axis = 0)
        
        res += res_arr
    return res / len(models_w_arr)


# In[5]:


def test_augment(model, models_w_arr, loader, n_imgs, device):
    res = np.zeros((n_imgs, 1), dtype = np.float32)
    for weights in models_w_arr:
        model.load_state_dict(weights) 
        model.eval()
        res_arr = []
        for inputs, _ in tqdm(loader):
            inputs = inputs.to(device)
            sz = inputs.shape
            inputs = inputs.reshape(-1,sz[2],sz[3],sz[4])
            # forward
            with torch.set_grad_enabled(False):
                outputs = model(inputs) 
                outputs = outputs.reshape(sz[0], sz[1], 1).mean(1)
                res_arr.append(outputs.detach().cpu().numpy())
        res_arr = np.concatenate(res_arr, axis = 0)
        
        res += res_arr
    return res / len(models_w_arr)


# In[6]:


def test_augment_feat(model, models_w_arr, loader, n_imgs, device):
    res = np.zeros((n_imgs, 2048), dtype = np.float32)
    for weights in models_w_arr:
        model.load_weights(weights) 
        model.eval()
        res_arr = []
        for inputs, _ in tqdm(loader):
            inputs = inputs.to(device)
            sz = inputs.shape
            inputs = inputs.reshape(-1,sz[2],sz[3],sz[4])
            # forward
            with torch.set_grad_enabled(False):
                outputs = model.extract_features(inputs) 
                outputs = outputs.reshape(sz[0], sz[1], -1).mean(1)
                res_arr.append(outputs.detach().cpu().numpy())
        res_arr = np.concatenate(res_arr, axis = 0)
        
        res += res_arr
    return res / len(models_w_arr)


# In[7]:


class WindDataset(Dataset):
    def __init__(self, imgs, gts, storm_ids, split_type, index, transform):
        if split_type == 'test':
            self.imgs = imgs   
        else:
            self.imgs = [imgs[idx] for idx in index]
            self.gts = [gts[idx] for idx in index]  
            self.storm_ids = storm_ids[index]
        
        self.split_type = split_type
        self.transform = transform
        
    def __len__(self):
        return len(self.imgs)
    
    def create_seq(self, i):
        idx_arr = [i]
        for j in range(i-1, i-time_steps-1, -1):
            if self.storm_ids[j] == self.storm_ids[i]:
                idx_arr.append(j)
            else:
                idx_arr.append(idx_arr[-1])

        if (self.split_type == 'train') and (np.random.rand() > 0.5):
            j = np.random.randint(1, time_steps-1)
            if np.random.rand() > 0.5:
                idx_arr = idx_arr[:j] + idx_arr[j+1:]
            else:
                idx_arr = idx_arr[:j+1] + idx_arr[j:-1]
        else:
            idx_arr = idx_arr[:-1]
        
        img_arr = [np.array(self.imgs[j]) for j in idx_arr]
        img_arr = [Image.fromarray(np.array(img_arr[j:j+3]).transpose(1,2,0)[:,:,ch_arr]) for j in range(0, time_steps, 3)]
        return img_arr

    def __getitem__(self, idx):
        #img = self.imgs[idx]
        img_arr = self.create_seq(idx)
        imgs = torch.cat([self.transform(img) for img in img_arr], 0)
        gt = self.gts[idx]
        return imgs, gt


# In[8]:


class WindTestDataset(Dataset):
    def __init__(self, imgs, transform):
        self.imgs = imgs 
        
        self.transform = transform
        
    def __len__(self):
        return len(self.imgs)

    def __getitem__(self, idx):
        img_arr = [Image.fromarray(self.imgs[idx][:,:,i:i+3][:,:,ch_arr]) for i in range(0, time_steps, 3)]

        aug_img_arr = []

        for angle in range(0,179,45):
            temp_arr = []
            for img in img_arr:
                rot_img = torchvision.transforms.functional.rotate(img, angle, False, False, None, None)
                rot_img = self.transform(rot_img)
                temp_arr.append(rot_img)
            imgs = torch.cat(temp_arr)
            aug_img_arr.append(imgs.unsqueeze(0))

        for angle in range(-45,-179,-45):
            temp_arr = []
            for img in img_arr:
                rot_img = torchvision.transforms.functional.rotate(img, angle, False, False, None, None)
                rot_img = self.transform(rot_img)
                temp_arr.append(rot_img)
            imgs = torch.cat(temp_arr)
            aug_img_arr.append(imgs.unsqueeze(0))
            
        aug_imgs = torch.cat(aug_img_arr)
        return aug_imgs, 0.0


# In[9]:


class WindValDataset(Dataset):
    def __init__(self, imgs, gts, storm_ids, split_type, index, transform):
        if split_type == 'test':
            self.imgs = imgs   
        else:
            self.imgs = [imgs[idx] for idx in index]
            self.gts = [gts[idx] for idx in index]  
            self.storm_ids = storm_ids[index]
        
        self.split_type = split_type
        self.transform = transform
        
    def __len__(self):
        return len(self.imgs)
    
    def create_seq(self, i):
        idx_arr = [i]
        for j in range(i-1, i-time_steps, -1):
            if self.storm_ids[j] == self.storm_ids[i]:
                idx_arr.append(j)
            else:
                idx_arr.append(idx_arr[-1])
                
        img_arr = [np.array(self.imgs[j]) for j in idx_arr]
        img_arr = [Image.fromarray(np.array(img_arr[j:j+3]).transpose(1,2,0)[:,:,ch_arr]) for j in range(0, time_steps, 3)]
        return img_arr

    def __getitem__(self, idx):
        img = self.imgs[idx]
        img_arr = self.create_seq(idx)
        
        aug_img_arr = []

        for angle in range(0,179,45):
            temp_arr = []
            for img in img_arr:
                rot_img = torchvision.transforms.functional.rotate(img, angle, False, False, None, None)
                rot_img = self.transform(rot_img)
                temp_arr.append(rot_img)
            imgs = torch.cat(temp_arr)
            aug_img_arr.append(imgs.unsqueeze(0))

        for angle in range(-45,-179,-45):
            temp_arr = []
            for img in img_arr:
                rot_img = torchvision.transforms.functional.rotate(img, angle, False, False, None, None)
                rot_img = self.transform(rot_img)
                temp_arr.append(rot_img)
            imgs = torch.cat(temp_arr)
            aug_img_arr.append(imgs.unsqueeze(0))
            
        aug_imgs = torch.cat(aug_img_arr)
        return aug_imgs, self.gts[idx]


# In[10]:


class ConvNet(nn.Module):
    def __init__(self):
        super(ConvNet, self).__init__()
        self.model_ft = models.resnet50(pretrained=True)
        num_ftrs = self.model_ft.fc.in_features
        #self.fc1 = nn.Sequential(nn.Linear(num_ftrs, 64), nn.ReLU())
        self.fc = nn.Sequential(nn.Linear(num_ftrs*time_steps//3, 64), nn.ReLU(), nn.Linear(64, 1))
        self.dp = nn.Dropout(0.2)
        
    def forward_conv(self, x):
        x = self.model_ft.conv1(x)
        x = self.model_ft.bn1(x)
        x = self.model_ft.relu(x)
        x = self.model_ft.maxpool(x)

        x = self.model_ft.layer1(x)
        x = self.model_ft.layer2(x)
        x = self.model_ft.layer3(x)
        x = self.model_ft.layer4(x)
        #print(x.shape)
        
        #x = (x * masks[:,None]).sum((-1,-2)) / masks[:,None].sum((-1,-2))

        x = self.model_ft.avgpool(x).squeeze(-1).squeeze(-1)
        x = self.dp(x)
        #x = self.fc1(x)
        return x
    
    def forward(self, x):
        #x1, x2, x3, x4 = x[:, :3], x[:, 3:6], x[:, 6:9], x[:, 9:]
        x1, x2, x3 = x[:, :3], x[:, 3:6], x[:, 6:9]
        x1 = self.forward_conv(x1)
        x2 = self.forward_conv(x2)
        x3 = self.forward_conv(x3)
        #x4 = self.forward_conv(x4)
        #x = torch.cat([x4, x3, x2, x1], 1)
        x = torch.cat([x3, x2, x1], 1)
        x = self.fc(x)
        return x


# In[11]:


time_steps = 9


# In[12]:


train_df = pd.read_csv('training_set_features.csv')
train_df = train_df.sort_values('image_id')
train_labels = pd.read_csv('training_set_labels.csv')
train_labels['wind_speed'] = train_labels['wind_speed'].astype(np.float32)
train_labels = train_labels.sort_values('image_id')

train_df = pd.concat([train_df, train_labels['wind_speed']], axis = 1)


# In[13]:


train_imgs = []
for img_id in train_df['image_id']:
    img = Image.open('re-train-images/train/%s.jpg'%(img_id))
    train_imgs.append(img.copy())
    img.close()
    train_imgs[-1] = np.array(train_imgs[-1])
    train_imgs[-1][train_imgs[-1] == 255] = 0
    train_imgs[-1] = Image.fromarray(train_imgs[-1])


# In[ ]:


#train_imgs = np.array(train_imgs).astype(np.uint8)


# In[ ]:


#train_imgs.shape


# In[ ]:


#train_imgs.mean(), train_imgs.std()


# In[14]:


#group_kfold = GroupShuffleSplit(n_splits=5, random_state = 4321)
group_kfold = GroupKFold(n_splits=5)


# In[12]:


data_transforms = {
    'train': transforms.Compose([
        transforms.Resize(224),
        #transforms.Grayscale(3),
        transforms.RandomAffine(degrees = 45, scale = (0.9,1.1)),
        transforms.RandomHorizontalFlip(p = 0.5),
        transforms.RandomVerticalFlip(p = 0.5),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ]),
    'val': transforms.Compose([
        transforms.Resize(224),
        #transforms.Grayscale(3),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
    ]),
}


# In[13]:


ch_arr = [0,1,2]


# In[ ]:


path = '9ts_imgs_time_aug_resnet50_models'
os.makedirs(path)


# In[21]:


models_w_arr = []
fold = 0
for train_index, val_index in group_kfold.split(train_df, train_df['wind_speed'], train_df['storm_id']):
    print(fold)
    fold += 1
    os.makedirs(path + '/fold_%d'%(fold-1))
    image_datasets = {'train': WindDataset(train_imgs, train_df['wind_speed'].values, train_df['storm_id'].values, 'train', train_index, data_transforms['train']),
                      'val': WindDataset(train_imgs, train_df['wind_speed'].values, train_df['storm_id'].values, 'val', val_index, data_transforms['val'])}

    dataloaders = {'train': torch.utils.data.DataLoader(image_datasets['train'], batch_size=32, shuffle=True, num_workers=12),
                   'val': torch.utils.data.DataLoader(image_datasets['val'], batch_size=8, shuffle=False, num_workers=4)}

    model_ft = ConvNet()
    model_ft = model_ft.to(device)
    
    criterion = nn.L1Loss()
    eval_criterion = nn.MSELoss()

    dataset_sizes = {x:len(image_datasets[x]) for x in ['train', 'val']}
    
    #train a model on this data split using snapshot ensemble
    model_ft_arr, _, _ = train_model_snapshot(model_ft, criterion, eval_criterion, 0.0002, dataloaders, dataset_sizes, device,
                           num_cycles=1, num_epochs_per_cycle=5)
    for i, w in enumerate(model_ft_arr):
        torch.save(w, path + '/fold_%d/%d.pth'%(fold-1, i))
    models_w_arr.extend(model_ft_arr)
    #break


# # Generate final predictions from Kfold models

# In[15]:


#path = '9ts_imgs_time_aug_resnet50_models'
#models_w_arr = []
#for fold in range(5):
#    models_w_arr.append(torch.load(path + '/fold_%d/0.pth'%(fold)))


# In[16]:


#model_ft = ConvNet()
#model_ft = model_ft.to(device)


# In[29]:


val_pred = np.zeros((train_df.shape[0], 1))

fold = 0

for _, val_index in group_kfold.split(train_df, train_df['wind_speed'], train_df['storm_id']):    
    val_dataset = WindValDataset(train_imgs, train_df['wind_speed'].values, train_df['storm_id'].values, 'val', val_index, data_transforms['val'])

    val_dataloader = torch.utils.data.DataLoader(val_dataset, batch_size=4, shuffle=False, num_workers=8)
    
    val_pred[val_index] = test_augment(model_ft, models_w_arr[fold:(fold+1)], val_dataloader, len(val_dataset), device)
    
    fold += 1
    #break


# In[30]:


np.sqrt(mean_squared_error(train_df['wind_speed'].values[val_index], val_pred[val_index]))


# In[25]:


train_df['pred'] = val_pred[:,0]
print(train_df.head())
train_df.to_csv(path + '/train.csv', index=False)


# In[17]:


test_df = pd.read_csv('test_set_features.csv')


# In[18]:


test_imgs = np.load('9ts_test_imgs_224.npy')
test_imgs_ids = np.load('9ts_test_imgs_ids.npy')


# In[19]:


test_dataset = WindTestDataset(test_imgs, data_transforms['val'])
test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=16,shuffle=False, num_workers=8)


# In[20]:


test_pred = test_augment(model_ft, models_w_arr, test_loader, len(test_dataset), device)


# In[21]:


sub = pd.read_csv('submission_format.csv')
sub['image_id'] = test_imgs_ids
sub['wind_speed'] = np.round(test_pred[:,0]).astype(np.int64)
sub.to_csv('9ts_imgs_res50_group_5folds_aug_time.csv', index = False)


# In[22]:


sub['pred'] = test_pred[:,0]
test_df = test_df.sort_values('image_id')
sub = sub.sort_values('image_id')
test_df = pd.concat([test_df, sub['pred']], axis = 1)
print(test_df.head())
test_df.to_csv(path + '/test.csv', index=False)


# In[ ]:




