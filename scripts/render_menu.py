from PIL import Image, ImageDraw, ImageFont, ImageEnhance
BG = "/media/servidor/d0a196c3-2d36-4431-b15d-8ea078ad8222/model-router-output/9d979bc444f14368aa0de823b1d4b0ac.png"
OUT = "/media/servidor/d0a196c3-2d36-4431-b15d-8ea078ad8222/model-router-output/menu-rediseno.png"
items = [
("Burguer eco especial x1","$6.000"),("Burguer eco especial x2","$10.000"),("Burguer eco especial x5","$20.000"),
("Burguer eco comun x1","$5.000"),("Burguer eco comun x2","$9.000"),("Burguer eco comun x5","$18.000"),
("Box 2 burguer eco especial + pizzeta + papas","$17.000"),("Hamburguesa Gera Especial","$12.000"),
("Hamburguesa Gera Especial x2","$18.000"),("Hamburguesa Gera Especial x4","$27.000"),
("Hamburguesa Gera Comun","$10.000"),("Hamburguesa Gera Comun x2","$16.000"),("Hamburguesa Gera Comun x4","$24.000"),
("Cool Panceta y Cheddar","$15.000"),("2 burguer gera especial + pizzeta + papas","$22.000"),
("Docena Carne Frita","$15.000"),("Docena Criolla Frita","$15.000"),("Docena JyQ Frita","$15.000"),
("Docena Carne Horno","$15.000"),("Docena Criolla Horno","$15.000"),("Docena JyQ Horno","$15.000"),
("Media Docena Carne Frita","$8.000"),("Media Docena Criolla Frita","$8.000"),("Media Docena JyQ Frita","$8.000"),
("Media Docena Carne Horno","$8.000"),("Media Docena Criolla Horno","$8.000"),("Media Docena JyQ Horno","$8.000"),
("Sandwich de lomo baguette entero con papas","$30.000"),("Medio baguette con papas","$18.000"),
("Sandwich de lomo con papas (pan redondo)","$12.000"),("Sándwich de Mila pan baguette entero + papas","$25.000"),
("Media Milanesa","$16.000"),("Sándwich de Mila pan redondo + papas","$11.000"),
("Medio Lomo + Pizzeta + Papas","$23.000"),("Media Mila + Pizzeta + Papas","$20.000"),
("Triple Tostado","$15.000"),("Triple Frío","$13.000"),("Triple Lomo","$25.000"),("Triple Lomo Gratinado","$27.000"),
("1/2 Triple Lomo+Pizzeta+Papas","$18.000"),("Pizza Muzza","$13.000"),("Napo","$13.000"),("Especial","$13.000"),
("Calabresa","$13.000"),("Arcoiris","$13.000"),("Pizza Lomo con papas grande","$25.000"),("Pizzeta","$8.000"),
("Pizzeta Lomo","$16.000"),("Papas","$4.000"),("Papas Grande","$7.000"),("Papas Cheddar y Panceta","$12.000"),
("Picada para 4 personas","$25.000"),
]
def font(s, b=True):
    return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if b else ""), s)
img = Image.open(BG).convert("RGB")
W, H = img.size
img = ImageEnhance.Brightness(img).enhance(0.35)  # oscurece para legibilidad
d = ImageDraw.Draw(img, "RGBA")
d.rectangle([0, 0, W, H], fill=(0, 0, 0, 110))
y = 30
f = font(64)
t = "NUEVOS PRECIOS"
bb = d.textbbox((0,0), t, font=f, stroke_width=2)
d.text(((W-(bb[2]-bb[0]))/2, y), t, font=f, fill=(255,200,60), stroke_width=2, stroke_fill=(0,0,0))
y += 100
col_w = (W - 90) // 2
fs = 25
fi, fp = font(fs, False), font(fs)
half = (len(items) + 1) // 2
for col in range(2):
    x0 = 30 + col * (col_w + 30)
    yy = y
    for i in range(half):
        idx = col * half + i
        if idx >= len(items): break
        name, price = items[idx]
        num = f"{idx+1}. "
        # recorta nombre si no entra
        maxn = col_w - 130
        while d.textlength(num + name, font=fi) > maxn and len(name) > 12:
            name = name[:-2]
        d.text((x0, yy), num + name, font=fi, fill=(255,255,255))
        pb = d.textbbox((0,0), price, font=fp)
        d.text((x0 + col_w - (pb[2]-pb[0]), yy), price, font=fp, fill=(255,220,120))
        yy += 40
y0 = y + half * 40 + 20
f2 = font(40)
c = "Escribinos 3624782201"
bb = d.textbbox((0,0), c, font=f2, stroke_width=2)
tw = bb[2]-bb[0]
d.rounded_rectangle([(W-tw)/2-24, y0, (W+tw)/2+24, y0+86], radius=26, fill=(20,140,60))
d.text(((W-tw)/2, y0+18), c, font=f2, fill=(255,255,255), stroke_width=2, stroke_fill=(0,0,0))
img.save(OUT)
print("OK", OUT, img.size)
