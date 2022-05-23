## Installation

docker pull  junhyung5544/my_openpcdet:spconv_2_1_21_python3_6

make container mounting your workspace

git clone https://github.com/konyul/actr_fpcdet_rasd3.git

git clone https://github.com/open-mmlab/mmdetection3d.git in the fusion_openpcdet repository


fusion_openpcdet repository,

{
  
  pip install -r requirement.txt

  python setup.py develop
  
}

mmdetection3d repository,

{

  pip install -v -e.

}

fusion_openpcdet repository

{
  
  python setup.py develop

  pip install mmcv-full==1.4.8 -f https://download.openmmlab.com/mmcv/dist/cu111/torch1.8.0/index.html

  pip install mmdet==2.19.0

  pip install mmsegmentation==0.20.0

  apt-get install libgl1-mesa-glx
  
  pip install kornia
  
}


## Citation 
If you find this project useful in your research, please consider citing:

```
@inproceedings{focalsconv-chen,
  title={Focal Sparse Convolutional Networks for 3D Object Detection},
  author={Chen, Yukang and Li, Yanwei and Zhang, Xiangyu and Sun, Jian and Jia, Jiaya},
  booktitle={Proceedings of the IEEE Conference on Computer Vision and Pattern Recognition},
  year={2022}
}
```

## Acknowledgement
-  This work is built upon the `OpenPCDet` and `CenterPoint`. Please refer to the official github repositories, [OpenPCDet](https://github.com/open-mmlab/OpenPCDet) and [CenterPoint](https://github.com/tianweiy/CenterPoint) for more information.

-  This README follows the style of [IA-SSD](https://github.com/yifanzhang713/IA-SSD).



## License

This project is released under the [Apache 2.0 license](LICENSE).


## Related Repos
1. [spconv](https://github.com/traveller59/spconv) ![GitHub stars](https://img.shields.io/github/stars/traveller59/spconv.svg?style=flat&label=Star)
2. [Deformable Conv](https://github.com/msracver/Deformable-ConvNets) ![GitHub stars](https://img.shields.io/github/stars/msracver/Deformable-ConvNets.svg?style=flat&label=Star)
3. [Submanifold Sparse Conv](https://github.com/facebookresearch/SparseConvNet) ![GitHub stars](https://img.shields.io/github/stars/facebookresearch/SparseConvNet.svg?style=flat&label=Star)
