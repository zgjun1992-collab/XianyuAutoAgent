"""Generate deterministic Windows app/tray icons for the V3.6 desktop client."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "assets" / "xianyu-ai.ico"
PREVIEW = ROOT / "assets" / "xianyu-ai.png"
CANVAS = 1024


def load_font(size: int):
    for path in (
        Path("C:/Windows/Fonts/arialbd.ttf"),
        Path("C:/Windows/Fonts/segoeuib.ttf"),
    ):
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def build_icon():
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    yellow = "#FFD600"
    charcoal = "#171A17"
    draw.rounded_rectangle((40, 40, 984, 984), radius=220, fill=yellow)
    draw.ellipse((198, 198, 826, 826), fill=charcoal)

    font = load_font(330)
    label = "AI"
    box = draw.textbbox((0, 0), label, font=font)
    width, height = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((CANVAS - width) / 2, (CANVAS - height) / 2 - box[1] - 14),
        label,
        font=font,
        fill=yellow,
    )
    return image


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    icon = build_icon().resize((256, 256), Image.Resampling.LANCZOS)
    icon.save(PREVIEW, format="PNG", optimize=True)
    icon.save(
        OUTPUT,
        format="ICO",
        sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
               (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(OUTPUT)
    print(PREVIEW)


if __name__ == "__main__":
    main()
