import numpy
from tqdm import tqdm
from sklearn.decomposition import MiniBatchDictionaryLearning, SparseCoder
from skimage.measure import compare_ssim
from sklearn.metrics import average_precision_score, roc_auc_score
import pickle
import skimage
import os
import matplotlib.pyplot as plt
import cv2


class SparseCodingWithMultiDict(object):
    def __init__(self, preprocesses, num_of_basis, alpha, transform_algorithm, transform_alpha, fit_algorithm, n_iter, num_of_nonzero, train_loader=None, test_neg_loader=None, test_pos_loader=None, use_ssim=False):
        self.preprocesses = preprocesses

        self.num_of_basis = num_of_basis
        self.alpha = alpha
        self.transform_algorithm = transform_algorithm
        self.transform_alpha = transform_alpha
        self.fit_algorithm = fit_algorithm
        self.n_iter = n_iter
        self.num_of_nonzero = num_of_nonzero

        self.train_loader = train_loader
        self.test_neg_loader = test_neg_loader
        self.test_pos_loader = test_pos_loader

        self.use_ssim = use_ssim

        self.Mu = None
        self.Sigma = None
        self.dictionaries = None

    def train(self):
        arrs = []
        for batch_img in self.train_loader:
            for p in self.preprocesses:
                batch_img = p(batch_img)
            N, P, C, H, W = batch_img.shape
            batch_arr = batch_img.reshape(N*P, C, H*W)
            arrs.append(batch_arr)

        train_arr = numpy.concatenate(arrs, axis=0)

        self.dictionaries = [MiniBatchDictionaryLearning(n_components=self.num_of_basis, alpha=self.alpha, transform_algorithm=self.transform_algorithm, transform_alpha=self.transform_alpha, fit_algorithm=self.fit_algorithm, n_iter=self.n_iter).fit(train_arr[i]).components_ for i in tqdm(range(C), desc='learning dictionary')]
        print("learned.")

    def save_dict(self, file_path):
        with open(file_path, 'wb') as f:
            pickle.dump(self.dictionaries, f)

    def load_dict(self, file_path):
        with open(file_path, 'rb') as f:
            self.dictionaries = pickle.load(f)

    def test(self):
        C = len(self.dictionaries)
        coders = [SparseCoder(dictionary = self.dictionaries[i], transform_algorithm=self.transform_algorithm, transform_n_nonzero_coefs=self.num_of_nonzero) for i in range(C)]

        neg_err = self.calculate_error(coders=coders, mode='neg', desc='testing for negative sample')
        pos_err = self.calculate_error(coders=coders, mode='pos', desc='testing for positive sample')

        ap, auc = self.calculate_score(neg_err, pos_err)
        print('\nTest set: AP: {:.4f}, AUC: {:.4f}\n'.format(ap, auc))

    def calculate_error(self, coders, mode, desc=None):
        if mode == 'neg':
            loader = self.test_neg_loader
        elif mode == 'pos':
            loader = self.test_pos_loader
        else:
            raise ValueError("The argument 'mode' must be set to 'neg' or 'pos'.")

        errs = []
        top_5 = numpy.zeros(len(self.dictionaries))
        for batch_img in tqdm(loader, desc=desc):
            for p in self.preprocesses:
                batch_img = p(batch_img)

            for img in batch_img:
                P, C, H, W = img.shape
                img_arr = img.reshape(P, C, H*W)

                ch_err = []
                for i in range(C):
                    target_arr = img_arr[:,i]
                    coefs = coders[i].transform(target_arr) 
                    rcn_arr = coefs.dot(self.dictionaries[i])

                    if not self.use_ssim:
                        err = numpy.sum((target_arr - rcn_arr)**2, axis=1)
                    else:
                        err = [-1*compare_ssim(img_arr[n,p,c].reshape(H,W), rcn_arr[n,p,c].reshape(H,W), win_size=11, data_range=1., gaussian_weights=True) for n in range(N) for p in range(P) for c in range(C)]
                    sorted_err = numpy.sort(err)[::-1]
                    total_err = numpy.sum(sorted_err[:5])

                    ch_err.append(total_err)
                top_5[numpy.argsort(ch_err)[::-1][:5]]+=1
  
                errs.append(numpy.sum(ch_err))

        return errs

    def calculate_score(self, dn, dp):
        N = len(dn)
        y_score = numpy.concatenate([dn, dp])
        y_true = numpy.zeros(len(y_score), dtype=numpy.int32)
        y_true[N:] = 1
        return average_precision_score(y_true, y_score), roc_auc_score(y_true, y_score)


    def visualize(self, ch, org_H, org_W, patch_size, stride):
        C = len(self.dictionaries)
        coder = SparseCoder(dictionary = self.dictionaries[ch], transform_algorithm=self.transform_algorithm, transform_n_nonzero_coefs=self.num_of_nonzero)
        
        self.visualize_features(coder=coder, mode='neg', ch=ch, org_H=org_H, org_W=org_W, patch_size=patch_size, stride=stride, desc='visualizing for negative sample')
        #self.visualize_features(coder=coder, mode='pos', ch=ch, org_H=org_H, org_W=org_W, patch_size=patch_size, stride=stride, desc='visualizing for positive sample')

    def visualize_features(self, coder, mode, ch, org_H, org_W, patch_size, stride, desc=None):
        if mode == 'neg':
            loader = self.test_neg_loader
        elif mode == 'pos':
            loader = self.test_pos_loader
        else:
            raise ValueError("The argument 'mode' must be set to 'neg' or 'pos'.")

        for idx, batch_img in tqdm(enumerate(loader), desc=desc):
            p_batch_img = batch_img
            for p in self.preprocesses:
                p_batch_img = p(p_batch_img)

            for img, img_org in zip(p_batch_img, batch_img):
                P, C, H, W = img.shape
                img_arr = img.reshape(P, C, H*W)
                f_diff = numpy.zeros((1, org_H, org_W))

                for i in range(C):
                    target_arr = img_arr[:,i]
                    coefs = coder.transform(target_arr) 
                    rcn_arr = coefs.dot(self.dictionaries[i])
                    f_img_org = self.reconst_from_array(target_arr, org_H, org_W, patch_size, stride)
                    f_img_rcn = self.reconst_from_array(rcn_arr, org_H, org_W, patch_size, stride)
                    f_diff += numpy.square((f_img_org - f_img_rcn) / 2)

                f_diff /= C
                color_map = plt.get_cmap('viridis')
                heatmap = numpy.uint8(color_map(f_diff[0])[:, :, :3] * 255)
                
                transposed = img_org.transpose(1, 2, 0)
                resized = cv2.resize(heatmap, (transposed.shape[0], transposed.shape[1]))
                blended = cv2.addWeighted(transposed, 1.0, resized, 0.01, 2.2, dtype = cv2.CV_32F)
                blended_normed = 255 * (blended - blended.min()) / (blended.max() - blended.min())
                blended_out = numpy.array(blended_normed, numpy.int)
                
                output_path = os.path.join('visualized_results', mode)
                os.makedirs(output_path, exist_ok=True)

                err = numpy.sum((target_arr-rcn_arr)**2, axis=1)
                sorted_err = numpy.sort(err)[::-1]
                total_err = numpy.sum(sorted_err[:5])
                cv2.imwrite(os.path.join(output_path, str(idx)+'-'+str(total_err)+'.png'), blended_out)

    def reconst_from_array(self, arrs, org_H, org_W, patch_size, stride):
        rcn = numpy.zeros((1, org_H, org_W))
        arr_iter = iter(arrs)
        for ty in range(0, org_H-patch_size+1, stride):
            for tx in range(0, org_W-patch_size+1, stride):
                arr = next(arr_iter)
                rcn[:, ty:ty+patch_size, tx:tx+patch_size] = arr.reshape(1, patch_size, patch_size)
        return rcn
