Place three PNG icon files here before loading the extension in Chrome:

  icon16.png   — 16×16 pixels
  icon48.png   — 48×48 pixels
  icon128.png  — 128×128 pixels

Quick way to generate them (requires Pillow):

  pip install Pillow

  python -c "
  from PIL import Image, ImageDraw
  for size in [16, 48, 128]:
      img = Image.new('RGBA', (size, size), (0, 212, 170, 255))
      d = ImageDraw.Draw(img)
      d.ellipse([size//4, size//4, 3*size//4, 3*size//4], fill=(15, 17, 23, 255))
      img.save(f'icon{size}.png')
  print('Icons generated.')
  "

Run the above command from this icons/ directory.
