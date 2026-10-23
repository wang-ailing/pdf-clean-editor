from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parent
im = Image.new("RGBA", (256, 256))
d = ImageDraw.Draw(im)
d.rounded_rectangle((8, 8, 248, 248), radius=54, fill="#087e78")
d.rounded_rectangle((58, 38, 199, 219), radius=12, fill="white")
d.line((84, 79, 170, 79), fill="#a9b7c3", width=10)
d.rounded_rectangle((76, 105, 180, 128), radius=4, fill="#ffdf55")
d.line((84, 116, 169, 116), fill="#576b79", width=6)
d.line((84, 151, 160, 151), fill="#a9b7c3", width=9)
d.polygon([(145, 168), (186, 128), (229, 171), (188, 211)], fill="#a8e0d6")
d.polygon([(145, 168), (164, 149), (207, 192), (188, 211)], fill="#f29f9f")
im.save(root / "editor.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
