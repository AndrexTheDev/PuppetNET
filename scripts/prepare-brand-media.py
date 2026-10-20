"""Offline asset preparation. pip install pillow imageio-ffmpeg numpy scipy
Usage: python scripts/prepare-brand-media.py /path/to/originals
Original files: Logo.jpg, Hintergrund.jpg, Intro.mp4 (never modified).
"""
from pathlib import Path
import sys, subprocess
from PIL import Image, ImageDraw, ImageEnhance
import numpy as np
from scipy.ndimage import binary_propagation, gaussian_filter
import imageio_ffmpeg

source = Path(sys.argv[1])
out = Path(__file__).resolve().parents[1] / 'apps/web/public/media'
out.mkdir(parents=True, exist_ok=True)
logo = Image.open(source/'Logo.jpg').convert('RGB')
a = np.array(logo)
# Only near-white pixels connected to the outside are background; preserve inner highlights.
nearwhite = (a.min(axis=2) > 205) & ((a.max(axis=2).astype(int)-a.min(axis=2)) < 35)
mask = Image.fromarray(np.where(nearwhite, 255, 0).astype('uint8')).copy()
for xy in [(0,0),(logo.width-1,0),(0,logo.height-1),(logo.width-1,logo.height-1)]:
    ImageDraw.floodfill(mask, xy, 128)
external = np.array(mask) == 128
alpha = np.full(a.shape[:2],255,dtype='uint8');alpha[external]=0
rgba=logo.convert('RGBA');rgba.putalpha(Image.fromarray(alpha))
bbox=rgba.getbbox();rgba=rgba.crop(bbox)
for size,name in [(640,'logo'),(192,'logo-icon')]:
    im=rgba.copy();im.thumbnail((size,size),Image.Resampling.LANCZOS)
    im.save(out/f'{name}.webp',quality=94,method=6)
rgba.thumbnail((512,512),Image.Resampling.LANCZOS);rgba.save(out/'logo.png',optimize=True)
# The radar artwork has no white exterior to key. Retain it, darken and feather
# edges into alpha instead of erasing luminous radar lines and map details.
bg=Image.open(source/'Hintergrund.jpg').convert('RGB')
bg.thumbnail((1600,2000),Image.Resampling.LANCZOS)
bg=ImageEnhance.Brightness(bg).enhance(.68).convert('RGBA')
w,h=bg.size;yy,xx=np.mgrid[:h,:w]
edge=np.minimum.reduce([xx/w,(w-1-xx)/w,yy/h,(h-1-yy)/h]);alpha=np.clip(edge/.07,0,1)
bg.putalpha(Image.fromarray((alpha*255).astype('uint8')));bg.save(out/'background.webp',quality=84,method=6)
ff=imageio_ffmpeg.get_ffmpeg_exe()
# Key only border-connected near-white regions, not the bright inner glow.
# Global chroma/luma keying would punch visible holes into the animated emblem.
size = 720
raw = subprocess.Popen([ff,'-v','error','-i',str(source/'Intro.mp4'),'-vf',
    'crop=720:720:0:280','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],stdout=subprocess.PIPE)
encode = subprocess.Popen([ff,'-v','error','-y','-f','rawvideo','-pix_fmt','rgba',
    '-s','720x720','-r','24','-i','pipe:0','-an','-c:v','libvpx-vp9','-pix_fmt','yuva420p',
    '-b:v','0','-crf','33','-deadline','good','-cpu-used','4','-auto-alt-ref','0',
    str(out/'intro-alpha.webm')],stdin=subprocess.PIPE)
try:
    while True:
        data = raw.stdout.read(size*size*3)
        if not data: break
        if len(data) != size*size*3: raise RuntimeError('Incomplete decoded frame')
        rgb = np.frombuffer(data,dtype=np.uint8).reshape(size,size,3)
        white = (rgb.min(2)>200) & ((rgb.max(2).astype(int)-rgb.min(2))<55)
        seed = np.zeros((size,size),bool)
        seed[0,:]=white[0,:];seed[-1,:]=white[-1,:]
        seed[:,0]=white[:,0];seed[:,-1]=white[:,-1]
        exterior = binary_propagation(seed,mask=white)
        alpha = (gaussian_filter((~exterior).astype(float),sigma=.45)*255).astype('uint8')
        encode.stdin.write(np.dstack((rgb,alpha)).tobytes())
finally:
    raw.stdout.close();encode.stdin.close()
if raw.wait() or encode.wait(): raise RuntimeError('Video conversion failed')
# MP4 cannot carry alpha: decode alpha explicitly, composite onto dark site colour.
subprocess.run([ff,'-v','error','-y','-c:v','libvpx-vp9','-i',str(out/'intro-alpha.webm'),
    '-filter_complex','color=c=0x0b100d:s=720x720:r=24:d=8[bg];[bg][0:v]overlay=shortest=1,format=yuv420p[v]',
    '-map','[v]','-an','-c:v','libx264','-crf','23','-preset','medium','-movflags','+faststart',str(out/'intro-fallback.mp4')],check=True)
print('Prepared:',[(p.name,p.stat().st_size) for p in out.iterdir()])
