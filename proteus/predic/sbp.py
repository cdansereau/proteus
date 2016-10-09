__author__ = 'Christian Dansereau'

import numpy as np
from sklearn.cluster import KMeans
from proteus.predic import clustering as cls
from proteus.matrix import tseries as ts
from proteus.predic import prediction
from proteus.predic import subtypes
from scipy.spatial.distance import pdist, squareform
from sklearn.cluster import MeanShift
from sklearn.neighbors.nearest_centroid import NearestCentroid
from sklearn import preprocessing
from sklearn.feature_selection import RFECV
from sklearn.svm import SVC
from sklearn.linear_model import LogisticRegression
from sklearn.grid_search import GridSearchCV
from sklearn.cross_validation import LeaveOneOut, LeavePOut, StratifiedKFold

from nistats import glm as nsglm
import statsmodels.stats.multitest as smm
import multiprocessing
import time

def compute_loo_parall((net_data_low_main,y,confounds,n_subtypes,train_index,test_index)):
    my_sbp = sbp()
    my_sbp.fit(net_data_low_main[train_index,...],y[train_index],confounds[train_index,...],n_subtypes,verbose=False)
    tmp_scores = my_sbp.predict(net_data_low_main[test_index,...],y[test_index],confounds[test_index,...])
    return np.hstack((y[test_index],tmp_scores[0][0],tmp_scores[1][0]))

class sbp:
    '''
    Pipeline for subtype base prediction
    '''
    def fit(self,net_data_low,y,confounds,n_subtypes=10,flag_feature_select=True,extra_var=[],verbose=True):
        ### regress confounds from the connectomes
        #net_data_low = net_data_low_main.copy()
        cf_rm = prediction.ConfoundsRm(confounds,net_data_low.reshape((net_data_low.shape[0],net_data_low.shape[1]*net_data_low.shape[2])))
        net_data_low_tmp = cf_rm.transform(confounds,net_data_low.reshape((net_data_low.shape[0],net_data_low.shape[1]*net_data_low.shape[2])))
        net_data_low = net_data_low_tmp.reshape((net_data_low_tmp.shape[0],net_data_low.shape[1],net_data_low.shape[2]))

        ### compute the subtypes
        if verbose: start = time.time()
        st_ = subtypes.clusteringST()
        st_.fit(net_data_low,n_subtypes)
        xw = st_.transform(net_data_low)
        #xw = np.hstack((age_var,xw))
        if verbose: print("Compute subtypes, Time elapsed: {}s)".format(int(time.time() - start)))

        ### feature selection
        if flag_feature_select:
            if verbose: start = time.time()
            contrast = np.hstack(([0,1],np.repeat(0,confounds.shape[1])))#[0,1,0,0,0]
            x_ = np.vstack((np.ones_like(y),y,confounds.T)).T

            labels, regression_result  = nsglm.session_glm(np.array(xw),x_)
            cont_results = nsglm.compute_contrast(labels,regression_result, contrast,contrast_type='t')
            pval = cont_results.p_value()
            results = smm.multipletests(pval, alpha=0.01, method='fdr_bh')
            w_select = np.where(results[0])[0]
            #w_select = w_select[np.argsort(pval[np.where(results[0])])]
            if len(w_select)<10:
                w_select = np.argsort(pval)[:10]
            else:
                w_select = w_select[np.argsort(pval[np.where(results[0])])]
        else:
            # Cancel the selection
            w_select = np.where(xw[0,:]!=2)[0]

        #w_select = get_stable_w(xw[train_index,:],y_tmp[train_index],confounds[train_index,:],6)
        # Cancel the selection
        #w_select = np.where(results[0]!=-1)[0]
        #print("Feature selected: {})".format(w_select))

        ### Include extra covariates
        if len(extra_var)!=0:
            all_var = np.hstack((xw[:,w_select],extra_var))
        else:
            all_var = xw[:,w_select]
        if verbose: print("Feature selection, Time elapsed: {}s)".format(int(time.time() - start)))

        ### prediction model
        if verbose: start = time.time()
        tlp = TwoLevelsPrediction()
        tlp.fit(all_var,y)
        if verbose: print("Two Levels prediction, Time elapsed: {}s)".format(int(time.time() - start)))

        ### save parameters
        self.median_template = np.median(net_data_low_main,axis=0)
        self.cf_rm = cf_rm
        self.st = st_
        self.w_select = w_select
        self.tlp = tlp

    def predict(self,net_data_low_main,y,confounds,extra_var=[]):
        ### regress confounds from the connectomes
        net_data_low = net_data_low_main.copy()
        net_data_low_tmp = self.cf_rm.transform(confounds,net_data_low.reshape((net_data_low.shape[0],net_data_low.shape[1]*net_data_low.shape[2])))
        net_data_low = net_data_low_tmp.reshape((net_data_low_tmp.shape[0],net_data_low.shape[1],net_data_low.shape[2]))

        ### subtypes w estimation
        self.xw = self.st.transform(net_data_low)

        ### Include extra covariates
        if len(extra_var)!=0:
            all_var = np.hstack((self.xw[:,self.w_select],extra_var))
        else:
            all_var = self.xw[:,self.w_select]

        ### prediction model
        return self.tlp.predict(all_var)

    def estimate_acc(self,net_data_low_main,y,confounds,n_subtypes=10):

        sss = LeaveOneOut(len(y))
        # scores: y, y_pred, decision_function
        self.scores = []
        k=0
        for train_index, test_index in sss:
            k+=1
            print('Fold: '+str(k)+'/'+str(len(y)))
            self.fit(net_data_low_main[train_index,...],y[train_index],confounds[train_index,...],verbose=False,flag_feature_select=False)
            tmp_scores = self.predict(net_data_low_main[test_index,...],y[test_index],confounds[test_index,...])
            self.scores.append(np.hstack((y[test_index],tmp_scores[0][0],tmp_scores[0][1])))
        self.scores = np.array(self.scores)

    def estimate_acc_multicore(self,net_data_low_main,y,confounds,n_subtypes=10):
        taskList_loo = []
        sss = LeaveOneOut(len(y))
        # scores: y, y_pred, decision_function
        self.scores = []
        k=0
        for train_index, test_index in sss:

            taskList_loo.append((net_data_low_main,y,confounds,n_subtypes,train_index,test_index))

        pool = multiprocessing.Pool(processes=(multiprocessing.cpu_count() - 2)) #Don't use all my processing power.
        r2 = pool.map_async(compute_loo_parall, taskList_loo, callback=self.scores.append)  #Using fxn "calculate", feed taskList, and values stored in "results" list
        r2.wait()
        pool.terminate()
        pool.join()
        self.scores = np.array(self.scores)

class TwoLevelsPrediction:
    '''
    2 Level prediction
    '''

    def fit(self,x,y,gs=10,model_type='logit'):
        xw = x.copy()
        if model_type=='logit':
            clf = LogisticRegression(C=1,class_weight='balanced',penalty='l2')
        else:
            clf = SVC(kernel='linear', class_weight='balanced', C=.1,probability=False)
        '''
        # wrapper feature selection
        rfecv = RFECV(estimator=clf, step=1, cv=StratifiedKFold(y, 3), scoring='f1')#accuracy
        rfecv.fit(xw, y)
        print("Optimal number of features : %d" % rfecv.n_features_)
        print("ids: {}".format((rfecv.ranking_<=5).sum()))
        print rfecv.grid_scores_
        self.rfecv = rfecv
        if rfecv.support_.sum()>10:
            self.w_select = rfecv.support_
        else:
            self.w_select = rfecv.ranking_<=10
        '''
        #xw = [:,self.w_select]

        #self.mask_selection = (np.ones((1,xw.shape[1]))==1)[0,:]
        ## Optimize the hyper parameters
        # Stage 1
        #param_grid = dict(C=(np.array([5,3,1])))
        if model_type=='logit':
            param_grid = dict(C=(10**np.arange(1.,-2.,-0.5)))
            #param_grid = dict(C=(np.arange(3,1,-0.5)))
        else:
            param_grid = dict(C=(np.arange(3.5,0.,-0.5)))
        #param_grid = dict(C=(np.array([0.01, 0.1, 1, 10, 100, 1000])))

        gridclf = GridSearchCV(clf, param_grid=param_grid, cv=StratifiedKFold(y,n_folds=gs), n_jobs=-1,scoring='accuracy')
        gridclf.fit(xw,y)
        clf = gridclf.best_estimator_
        print clf
        print clf.coef_
        hm_y,y_pred_train = estimate_hitmiss(clf,xw,y)

        print 'Stage 2'
        #Stage 2
        #clf2 = LogisticRegression(C=10**0.1,class_weight=None,penalty='l2',solver='sag')
        clf2 = LogisticRegression(C=1,class_weight=None,penalty='l2',solver='sag')
        param_grid = dict(C=(10**np.arange(1.,-2.,-0.5)))
        #param_grid = dict(C=(np.arange(3,1,-0.5)))

        # 2 levels balancing
        new_classes = np.zeros_like(y)
        new_classes[(y==0) & (hm_y==0)]=0
        new_classes[(y==1) & (hm_y==0)]=1
        new_classes[(y==0) & (hm_y==1)]=2
        new_classes[(y==1) & (hm_y==1)]=3

        tmp_samp_w = len(new_classes) / (len(np.unique(new_classes))*1. * np.bincount(new_classes))
        tmp_samp_w = (1.*(tmp_samp_w/tmp_samp_w.sum()))
        sample_w = new_classes.copy().astype(float)
        sample_w[new_classes==0] = tmp_samp_w[0]
        sample_w[new_classes==1] = tmp_samp_w[1]
        sample_w[new_classes==2] = tmp_samp_w[2]
        sample_w[new_classes==3] = tmp_samp_w[3]

        gridclf = GridSearchCV(clf2, param_grid=param_grid, cv=StratifiedKFold(hm_y,n_folds=gs),fit_params=dict(sample_weight=sample_w), n_jobs=-1,scoring='accuracy')
        #gridclf = GridSearchCV(clf2, param_grid=param_grid, cv=StratifiedKFold(hm_y,n_folds=gs), n_jobs=-1,scoring='accuracy')
        gridclf.fit(xw,hm_y)
        clf2 = gridclf.best_estimator_
        #clf2.fit(xw[train_index,:][:,idx_sz],hm_y)
        print clf2
        print clf2.coef_

        self.clf1 = clf
        self.clf2 = clf2

    def predict(self,x):
        xw = x.copy()#[:,self.w_select]
        y_pred1 = self.clf1.predict(xw)
        y_pred2 = self.clf2.decision_function(xw)
        return np.array([y_pred1,y_pred2]).T

def estimate_hitmiss(clf,x,y):
    #return clf.predict(x)==y,clf.predict(x)

    # Perform a LOO to estimate the actual HM
    label=1
    hm_results = []
    predictions =[]
    for i in range(len(y)):
        train_idx = np.array(np.hstack((np.arange(0,i),np.arange(i+1,len(y)))),dtype=int)
        clf.fit(x[train_idx,:],y[train_idx])
        #print clf.predict(x[i,:]) == y[i]
        hm_results.append(float(clf.predict(x[i,:].reshape(1,-1)) == y[i].reshape(1,-1)))
        predictions.append(clf.predict(x[i,:].reshape(1,-1)))
        #hm_results.append(int((y[i] == label) & (clf.predict(x[i,:]) == y[i]) ))#   clf.predict(x[i,:]) == y[i]))

    predictions = np.array(predictions)
    hm_results = np.array(hm_results)
    clf.fit(x,y)
    return hm_results, predictions[:,0]