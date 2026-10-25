#!/usr/bin/env python3
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from housing_segmentation.scans.synthetic import generate_scan
parser=argparse.ArgumentParser(description='Generate a multi-object scan and ZIP by CPU raycasting')
parser.add_argument('folder',type=Path)
parser.add_argument('--frames',type=int,default=120)
parser.add_argument('--width',type=int,default=320)
parser.add_argument('--version',type=int,choices=(1,2),default=1)
args=parser.parse_args()
generate_scan(args.folder,frames=args.frames,width=args.width,height=args.width*3//4,version=args.version)
print(args.folder.resolve())
