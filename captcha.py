"""Render a code as PNG without writing it to disk."""

from io import BytesIO
import secrets

from PIL import Image, ImageDraw, ImageFont


def render_captcha(code: str) -> BytesIO:
    random = secrets.SystemRandom()
    canvas = Image.new("RGB", (330, 110), "#edf2fa")
    draw = ImageDraw.Draw(canvas)
    for _ in range(350):
        x, y = random.randrange(330), random.randrange(110)
        draw.point((x, y), fill="#a9b8ce")
    font = ImageFont.load_default(size=42)
    for index, letter in enumerate(code):
        tile = Image.new("RGBA", (60, 75))
        ImageDraw.Draw(tile).text((10, 6), letter, font=font, fill="#263c65")
        tile = tile.rotate(random.randint(-18, 18), Image.Resampling.BICUBIC)
        canvas.paste(tile, (10 + index * 51, random.randint(15, 27)), tile)
    for _ in range(3):
        draw.line(
            (0, random.randrange(110), 330, random.randrange(110)),
            fill="#8395b2", width=1,
        )
    result = BytesIO()
    canvas.save(result, format="PNG")
    result.seek(0)
    return result
