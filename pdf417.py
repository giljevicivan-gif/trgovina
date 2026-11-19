# -*- coding: utf-8 -*-
"""
PDF417 (ISO/IEC 15438) u čistom Pythonu, za 2D barkod na uplatnici (HUB-3, "HRVHUB30").

Samo ono što treba za HUB-3: binarno (byte) sažimanje, razina ispravka pogrešaka 0-8,
zadani broj stupaca i crtanje u SVG. Tablica uzoraka kodnih riječi (3 klastera x 929)
preuzeta je iz projekta pdf417-py (Ivan Habunek, MIT licenca):

    Copyright (c) 2016 Ivan Habunek. Permission is hereby granted, free of charge, to any
    person obtaining a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including without limitation
    the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is furnished to do
    so, subject to the following conditions: The above copyright notice and this permission
    notice shall be included in all copies or substantial portions of the Software.
    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Ostatak (sažimanje, Reed-Solomon nad GF(929), indikatori redaka, SVG) napisan je ovdje.
"""

START = 0x1FEA8   # 17 modula
STOP = 0x3FA29    # 18 modula
_TAB = None


def _tablica():
    global _TAB
    if _TAB is None:
        _TAB = [[0x10000 | int(t[i:i + 4], 16) for i in range(0, len(t), 4)]
                for t in ("".join(k.split()) for k in _KLASTERI)]
    return _TAB


def _bajtovi(data):
    """Byte compaction: 924 kad je duljina djeljiva sa 6, inače 901; 6 bajtova -> 5 kodnih riječi."""
    cw = [924 if len(data) % 6 == 0 else 901]
    i = 0
    while i + 6 <= len(data):
        n = int.from_bytes(data[i:i + 6], "big")
        grupa = []
        for _ in range(5):
            n, r = divmod(n, 900)
            grupa.append(r)
        cw += reversed(grupa)
        i += 6
    cw += list(data[i:])
    return cw


def _rs_koeficijenti(k):
    """g(x) = (x - 3)(x - 3^2)...(x - 3^k) nad GF(929); vraća [a0, a1, ..., a(k-1)]."""
    g = [1]  # od najvišeg stupnja
    a = 1
    for _ in range(k):
        a = a * 3 % 929
        novi = g + [0]
        for j in range(len(g)):
            novi[j + 1] = (novi[j + 1] - a * g[j]) % 929
        g = novi
    return list(reversed(g[1:]))  # bez vodećeg 1, od a0


def _ispravak(podaci, razina):
    k = 2 ** (razina + 1)
    a = _rs_koeficijenti(k)
    e = [0] * k
    for d in podaci:
        t = (d + e[k - 1]) % 929
        for j in range(k - 1, 0, -1):
            e[j] = (e[j - 1] + 929 - t * a[j] % 929) % 929
        e[0] = (929 - t * a[0] % 929) % 929
    return [(929 - x) % 929 for x in reversed(e)]


def kodne_rijeci(data, stupaca=9, razina=4):
    """Kodne riječi (podaci + ispuna + ispravak) i broj redaka."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    podaci = _bajtovi(bytes(data))
    k = 2 ** (razina + 1)
    ukupno = 1 + len(podaci) + k
    redaka = max(3, -(-ukupno // stupaca))
    if redaka > 90 or redaka * stupaca > 928 + k:
        raise ValueError("Previše podataka za PDF417.")
    ispuna = redaka * stupaca - ukupno
    niz = [1 + len(podaci) + ispuna] + podaci + [900] * ispuna
    return niz + _ispravak(niz, razina), redaka


def matrica(data, stupaca=9, razina=4):
    """Lista redaka; svaki redak je lista bitova (1 = crno), bez tihe zone."""
    tab = _tablica()
    cw, redaka = kodne_rijeci(data, stupaca, razina)
    redovi = []
    for r in range(redaka):
        kl = r % 3
        if kl == 0:
            lijevi, desni = (redaka - 1) // 3, stupaca - 1
        elif kl == 1:
            lijevi, desni = razina * 3 + (redaka - 1) % 3, (redaka - 1) // 3
        else:
            lijevi, desni = stupaca - 1, razina * 3 + (redaka - 1) % 3
        baza = 30 * (r // 3)
        bitovi = []

        def dodaj(v, n=17):
            bitovi.extend((v >> (n - 1 - i)) & 1 for i in range(n))
        dodaj(START)
        dodaj(tab[kl][baza + lijevi])
        for w in cw[r * stupaca:(r + 1) * stupaca]:
            dodaj(tab[kl][w])
        dodaj(tab[kl][baza + desni])
        dodaj(STOP, 18)
        redovi.append(bitovi)
    return redovi


def svg(data, stupaca=9, razina=4, modul_mm=0.254, visina_retka=3, tiha=2, sirina_mm=None):
    """SVG barkoda; modul 0,254 mm i redak 3 modula visok kao u HUB-3 preporuci."""
    redovi = matrica(data, stupaca, razina)
    w = len(redovi[0]) + 2 * tiha
    h = len(redovi) * visina_retka + 2 * tiha
    put = []
    for y, red in enumerate(redovi):
        x = 0
        while x < len(red):
            if red[x]:
                p = x
                while x < len(red) and red[x]:
                    x += 1
                put.append("M%d %dh%dv%dh-%dz" % (p + tiha, y * visina_retka + tiha, x - p, visina_retka, x - p))
            else:
                x += 1
    sw = sirina_mm or w * modul_mm
    sh = sw * h / w
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%.2fmm" height="%.2fmm" '
            'shape-rendering="crispEdges"><rect width="%d" height="%d" fill="#fff"/>'
            '<path fill="#000" d="%s"/></svg>' % (w, h, sw, sh, w, h, "".join(put)))


_KLASTERI = (
    """
d5c0 eaf0 f57c d4e0 ea78 f53e a8c0 d470 a860 5040 a830 5020 adc0 d6f0 eb7c ace0 d678 eb3e 58c0 ac70
5860 5dc0 aef0 d77c 5ce0 ae78 d73e 5c70 ae3c 5ef0 af7c 5e78 af3e 5f7c f5fa d2e0 e978 f4be a4c0 d270
e93c a460 d238 4840 a430 d21c 4820 a418 4810 a6e0 d378 e9be 4cc0 a670 d33c 4c60 a638 d31e 4c30 a61c
4ee0 a778 d3be 4e70 a73c 4e38 a71e 4f78 a7be 4f3c 4f1e a2c0 d170 e8bc a260 d138 e89e 4440 a230 d11c
4420 a218 4410 4408 46c0 a370 d1bc 4660 a338 d19e 4630 a31c 4618 460c 4770 a3bc 4738 a39e 471c 47bc
a160 d0b8 e85e 4240 a130 d09c 4220 a118 d08e 4210 a10c 4208 a106 4360 a1b8 d0de 4330 a19c 4318 a18e
430c 4306 a1de 438e 4140 a0b0 d05c 4120 a098 d04e 4110 a08c 4108 a086 4104 41b0 4198 418c 40a0 d02e
a04c a046 4082 cae0 e578 f2be 94c0 ca70 e53c 9460 ca38 e51e 2840 9430 2820 96e0 cb78 e5be 2cc0 9670
cb3c 2c60 9638 2c30 2c18 2ee0 9778 cbbe 2e70 973c 2e38 2e1c 2f78 97be 2f3c 2fbe dac0 ed70 f6bc da60
ed38 f69e b440 da30 ed1c b420 da18 ed0e b410 da0c 92c0 c970 e4bc b6c0 9260 c938 e49e b660 db38 ed9e
6c40 2420 9218 c90e 6c20 b618 6c10 26c0 9370 c9bc 6ec0 2660 9338 c99e 6e60 b738 db9e 6e30 2618 6e18
2770 93bc 6f70 2738 939e 6f38 b79e 6f1c 27bc 6fbc 279e 6f9e d960 ecb8 f65e b240 d930 ec9c b220 d918
ec8e b210 d90c b208 b204 9160 c8b8 e45e b360 9130 c89c 6640 2220 d99c c88e 6620 2210 910c 6610 b30c
9106 2204 2360 91b8 c8de 6760 2330 919c 6730 b39c 918e 6718 230c 2306 23b8 91de 67b8 239c 679c 238e
678e 67de b140 d8b0 ec5c b120 d898 ec4e b110 d88c b108 d886 b104 b102 2140 90b0 c85c 6340 2120 9098
c84e 6320 b198 d8ce 6310 2108 9086 6308 b186 6304 21b0 90dc 63b0 2198 90ce 6398 b1ce 638c 2186 6386
63dc 63ce b0a0 d858 ec2e b090 d84c b088 d846 b084 b082 20a0 9058 c82e 61a0 2090 904c 6190 b0cc 9046
6188 2084 6184 2082 20d8 61d8 61cc 61c6 d82c d826 b042 902c 2048 60c8 60c4 60c2 8ac0 c570 e2bc 8a60
c538 1440 8a30 c51c 1420 8a18 1410 1408 16c0 8b70 c5bc 1660 8b38 c59e 1630 8b1c 1618 160c 1770 8bbc
1738 8b9e 171c 17bc 179e cd60 e6b8 f35e 9a40 cd30 e69c 9a20 cd18 e68e 9a10 cd0c 9a08 cd06 8960 c4b8
e25e 9b60 8930 c49c 3640 1220 cd9c c48e 3620 9b18 890c 3610 1208 3608 1360 89b8 c4de 3760 1330 cdde
3730 9b9c 898e 3718 130c 370c 13b8 89de 37b8 139c 379c 138e 13de 37de dd40 eeb0 f75c dd20 ee98 f74e
dd10 ee8c dd08 ee86 dd04 9940 ccb0 e65c bb40 9920 eedc e64e bb20 dd98 eece bb10 9908 cc86 bb08 dd86
9902 1140 88b0 c45c 3340 1120 8898 c44e 7740 3320 9998 ccce 7720 bb98 ddce 8886 7710 3308 9986 7708
1102 11b0 88dc 33b0 1198 88ce 77b0 3398 99ce 7798 bbce 1186 3386 11dc 33dc 11ce 77dc 33ce dca0 ee58
f72e dc90 ee4c dc88 ee46 dc84 dc82 98a0 cc58 e62e b9a0 9890 ee6e b990 dccc cc46 b988 9884 b984 9882
b982 10a0 8858 c42e 31a0 1090 884c 73a0 3190 98cc 8846 7390 b9cc 1084 7388 3184 1082 3182 10d8 886e
31d8 10cc 73d8 31cc 10c6 73cc 31c6 10ee 73ee dc50 ee2c dc48 ee26 dc44 dc42 9850 cc2c b8d0 9848 cc26
b8c8 dc66 b8c4 9842 b8c2 1050 882c 30d0 1048 8826 71d0 30c8 9866 71c8 b8e6 1042 71c4 30c2 71c2 30ec
71ec 71e6 ee16 dc22 cc16 9824 9822 1028 3068 70e8 1022 3062 8560 0a40 8530 0a20 8518 c28e 0a10 850c
0a08 8506 0b60 85b8 c2de 0b30 859c 0b18 858e 0b0c 0b06 0bb8 85de 0b9c 0b8e 0bde 8d40 c6b0 e35c 8d20
c698 8d10 c68c 8d08 c686 8d04 0940 84b0 c25c 1b40 0920 c6dc c24e 1b20 8d98 c6ce 1b10 0908 8486 1b08
8d86 0902 09b0 84dc 1bb0 0998 84ce 1b98 8dce 1b8c 0986 09dc 1bdc 09ce 1bce cea0 e758 f3ae ce90 e74c
ce88 e746 ce84 ce82 8ca0 c658 9da0 8c90 c64c 9d90 cecc c646 9d88 8c84 9d84 8c82 9d82 08a0 8458 19a0
0890 c66e 3ba0 1990 8ccc 8446 3b90 9dcc 0884 3b88 1984 0882 1982 08d8 846e 19d8 08cc 3bd8 19cc 08c6
3bcc 19c6 08ee 19ee 3bee ef50 f7ac ef48 f7a6 ef44 ef42 ce50 e72c ded0 ef6c e726 dec8 ef66 dec4 ce42
dec2 8c50 c62c 9cd0 8c48 c626 bdd0 9cc8 ce66 bdc8 dee6 8c42 bdc4 9cc2 bdc2 0850 842c 18d0 0848 8426
39d0 18c8 8c66 7bd0 39c8 9ce6 0842 7bc8 bde6 18c2 7bc4 086c 18ec 0866 39ec 18e6 7bec 39e6 7be6 ef28
f796 ef24 ef22 ce28 e716 de68 ef36 de64 ce22 de62 8c28 c616 9c68 8c24 bce8 9c64 8c22 bce4 9c62 bce2
0828 8416 1868 8c36 38e8 1864 0822 79e8 38e4 1862 79e4 38e2 79e2 1876 79f6 ef12 de34 de32 9c34 bc74
bc72 1834 3874 78f4 78f2 0540 0520 8298 0510 0508 0504 05b0 0598 058c 0586 05dc 05ce 86a0 8690 c34c
8688 c346 8684 8682 04a0 8258 0da0 86d8 824c 0d90 86cc 0d88 86c6 0d84 0482 0d82 04d8 826e 0dd8 86ee
0dcc 04c6 0dc6 04ee 0dee c750 c748 c744 c742 8650 8ed0 c76c c326 8ec8 c766 8ec4 8642 8ec2 0450 0cd0
0448 8226 1dd0 0cc8 0444 1dc8 0cc4 0442 1dc4 0cc2 046c 0cec 0466 1dec 0ce6 1de6 e7a8 e7a4 e7a2 c728
cf68 e7b6 cf64 c722 cf62 8628 c316 8e68 c736 9ee8 8e64 8622 9ee4 8e62 9ee2 0428 8216 0c68 8636 1ce8
0c64 0422 3de8 1ce4 0c62 3de4 1ce2 0436 0c76 1cf6 3df6 f7d4 f7d2 e794 efb4 e792 efb2 c714 cf34 c712
df74 cf32 df72 8614 8e34 8612 9e74 8e32 bef4
""",
    """
f560 fab8 ea40 f530 fa9c ea20 f518 fa8e ea10 f50c ea08 f506 ea04 eb60 f5b8 fade d640 eb30 f59c d620
eb18 f58e d610 eb0c d608 eb06 d604 d760 ebb8 f5de ae40 d730 eb9c ae20 d718 eb8e ae10 d70c ae08 d706
ae04 af60 d7b8 ebde 5e40 af30 d79c 5e20 af18 d78e 5e10 af0c 5e08 af06 5f60 afb8 d7de 5f30 af9c 5f18
af8e 5f0c 5fb8 afde 5f9c 5f8e e940 f4b0 fa5c e920 f498 fa4e e910 f48c e908 f486 e904 e902 d340 e9b0
f4dc d320 e998 f4ce d310 e98c d308 e986 d304 d302 a740 d3b0 e9dc a720 d398 e9ce a710 d38c a708 d386
a704 a702 4f40 a7b0 d3dc 4f20 a798 d3ce 4f10 a78c 4f08 a786 4f04 4fb0 a7dc 4f98 a7ce 4f8c 4f86 4fdc
4fce e8a0 f458 fa2e e890 f44c e888 f446 e884 e882 d1a0 e8d8 f46e d190 e8cc d188 e8c6 d184 d182 a3a0
d1d8 e8ee a390 d1cc a388 d1c6 a384 a382 47a0 a3d8 d1ee 4790 a3cc 4788 a3c6 4784 4782 47d8 a3ee 47cc
47c6 47ee e850 f42c e848 f426 e844 e842 d0d0 e86c d0c8 e866 d0c4 d0c2 a1d0 d0ec a1c8 d0e6 a1c4 a1c2
43d0 a1ec 43c8 a1e6 43c4 43c2 43ec 43e6 e828 f416 e824 e822 d068 e836 d064 d062 a0e8 d076 a0e4 a0e2
41e8 a0f6 41e4 41e2 e814 e812 d034 d032 a074 a072 e540 f2b0 f95c e520 f298 f94e e510 f28c e508 f286
e504 e502 cb40 e5b0 f2dc cb20 e598 f2ce cb10 e58c cb08 e586 cb04 cb02 9740 cbb0 e5dc 9720 cb98 e5ce
9710 cb8c 9708 cb86 9704 9702 2f40 97b0 cbdc 2f20 9798 cbce 2f10 978c 2f08 9786 2f04 2fb0 97dc 2f98
97ce 2f8c 2f86 2fdc 2fce f6a0 fb58 6bf0 f690 fb4c 69f8 f688 fb46 68fc f684 f682 e4a0 f258 f92e eda0
e490 fb6e ed90 f6cc f246 ed88 e484 ed84 e482 ed82 c9a0 e4d8 f26e dba0 c990 e4cc db90 edcc e4c6 db88
c984 db84 c982 db82 93a0 c9d8 e4ee b7a0 9390 c9cc b790 dbcc c9c6 b788 9384 b784 9382 b782 27a0 93d8
c9ee 6fa0 2790 93cc 6f90 b7cc 93c6 6f88 2784 6f84 2782 27d8 93ee 6fd8 27cc 6fcc 27c6 6fc6 27ee f650
fb2c 65f8 f648 fb26 64fc f644 647e f642 e450 f22c ecd0 e448 f226 ecc8 f666 ecc4 e442 ecc2 c8d0 e46c
d9d0 c8c8 e466 d9c8 ece6 d9c4 c8c2 d9c2 91d0 c8ec b3d0 91c8 c8e6 b3c8 d9e6 b3c4 91c2 b3c2 23d0 91ec
67d0 23c8 91e6 67c8 b3e6 67c4 23c2 67c2 23ec 67ec 23e6 67e6 f628 fb16 62fc f624 627e f622 e428 f216
ec68 f636 ec64 e422 ec62 c868 e436 d8e8 c864 d8e4 c862 d8e2 90e8 c876 b1e8 d8f6 b1e4 90e2 b1e2 21e8
90f6 63e8 21e4 63e4 21e2 63e2 21f6 63f6 f614 617e f612 e414 ec34 e412 ec32 c834 d874 c832 d872 9074
b0f4 9072 b0f2 20f4 61f4 20f2 61f2 f60a e40a ec1a c81a d83a 903a b07a e2a0 f158 f8ae e290 f14c e288
f146 e284 e282 c5a0 e2d8 f16e c590 e2cc c588 e2c6 c584 c582 8ba0 c5d8 e2ee 8b90 c5cc 8b88 c5c6 8b84
8b82 17a0 8bd8 c5ee 1790 8bcc 1788 8bc6 1784 1782 17d8 8bee 17cc 17c6 17ee f350 f9ac 35f8 f348 f9a6
34fc f344 347e f342 e250 f12c e6d0 e248 f126 e6c8 f366 e6c4 e242 e6c2 c4d0 e26c cdd0 c4c8 e266 cdc8
e6e6 cdc4 c4c2 cdc2 89d0 c4ec 9bd0 89c8 c4e6 9bc8 cde6 9bc4 89c2 9bc2 13d0 89ec 37d0 13c8 89e6 37c8
9be6 37c4 13c2 37c2 13ec 37ec 13e6 37e6 fba8 75f0 bafc fba4 74f8 ba7e fba2 747c 743e f328 f996 32fc
f768 fbb6 76fc 327e f764 f322 767e f762 e228 f116 e668 e224 eee8 f776 e222 eee4 e662 eee2 c468 e236
cce8 c464 dde8 cce4 c462 dde4 cce2 dde2 88e8 c476 99e8 88e4 bbe8 99e4 88e2 bbe4 99e2 bbe2 11e8 88f6
33e8 11e4 77e8 33e4 11e2 77e4 33e2 77e2 11f6 33f6 fb94 72f8 b97e fb92 727c 723e f314 317e f734 f312
737e f732 e214 e634 e212 ee74 e632 ee72 c434 cc74 c432 dcf4 cc72 dcf2 8874 98f4 8872 b9f4 98f2 b9f2
10f4 31f4 10f2 73f4 31f2 73f2 fb8a 717c 713e f30a f71a e20a e61a ee3a c41a cc3a dc7a 883a 987a b8fa
107a 30fa 71fa 70be e150 f0ac e148 f0a6 e144 e142 c2d0 e16c c2c8 e166 c2c4 c2c2 85d0 c2ec 85c8 c2e6
85c4 85c2 0bd0 85ec 0bc8 85e6 0bc4 0bc2 0bec 0be6 f1a8 f8d6 1afc f1a4 1a7e f1a2 e128 f096 e368 e124
e364 e122 e362 c268 e136 c6e8 c264 c6e4 c262 c6e2 84e8 c276 8de8 84e4 8de4 84e2 8de2 09e8 84f6 1be8
09e4 1be4 09e2 1be2 09f6 1bf6 f9d4 3af8 9d7e f9d2 3a7c 3a3e f194 197e f3b4 f192 3b7e f3b2 e114 e334
e112 e774 e332 e772 c234 c674 c232 cef4 c672 cef2 8474 8cf4 8472 9df4 8cf2 9df2 08f4 19f4 08f2 3bf4
19f2 3bf2 7af0 bd7c 7a78 bd3e 7a3c 7a1e f9ca 397c fbda 7b7c 393e 7b3e f18a f39a f7ba e10a e31a e73a
ef7a c21a c63a ce7a defa 843a 8c7a 9cfa bdfa 087a 18fa 39fa 7978 bcbe 793c 791e 38be 79be 78bc 789e
785e e0a8 e0a4 e0a2 c168 e0b6 c164 c162 82e8 c176 82e4 82e2 05e8 82f6 05e4 05e2 05f6 f0d4 0d7e f0d2
e094 e1b4 e092 e1b2 c134 c374 c132 c372 8274 86f4 8272 86f2 04f4 0df4 04f2 0df2 f8ea 1d7c 1d3e f0ca
f1da e08a e19a e3ba c11a c33a c77a 823a 867a 8efa 047a 0cfa 1dfa 3d78 9ebe 3d3c 3d1e 1cbe 3dbe 7d70
bebc 7d38 be9e 7d1c 7d0e 3cbc 7dbc 3c9e 7d9e 7cb8 be5e 7c9c 7c8e 3c5e 7cde 7c5c 7c4e 7c2e c0b4 c0b2
8174 8172 02f4 02f2 e0da c09a c1ba 813a 837a 027a 06fa 0ebe 1ebc 1e9e 3eb8 9f5e 3e9c 3e8e 1e5e 3ede
7eb0 bf5c 7e98 bf4e 7e8c 7e86 3e5c 7edc 3e4e 7ece 7e58 bf2e 7e4c 7e46 3e2e 7e6e 7e2c 7e26 0f5e 1f5c
1f4e 3f58 9fae 3f4c 3f46 1f2e 3f6e 3f2c 3f26
""",
    """
abe0 d5f8 53c0 a9f0 d4fc 51e0 a8f8 d47e 50f0 a87c 5078 fad0 5be0 adf8 fac8 59f0 acfc fac4 58f8 ac7e
fac2 587c f5d0 faec 5df8 f5c8 fae6 5cfc f5c4 5c7e f5c2 ebd0 f5ec ebc8 f5e6 ebc4 ebc2 d7d0 ebec d7c8
ebe6 d7c4 d7c2 afd0 d7ec afc8 d7e6 afc4 4bc0 a5f0 d2fc 49e0 a4f8 d27e 48f0 a47c 4878 a43e 483c fa68
4df0 a6fc fa64 4cf8 a67e fa62 4c7c 4c3e f4e8 fa76 4efc f4e4 4e7e f4e2 e9e8 f4f6 e9e4 e9e2 d3e8 e9f6
d3e4 d3e2 a7e8 d3f6 a7e4 a7e2 45e0 a2f8 d17e 44f0 a27c 4478 a23e 443c 441e fa34 46f8 a37e fa32 467c
463e f474 477e f472 e8f4 e8f2 d1f4 d1f2 a3f4 a3f2 42f0 a17c 4278 a13e 423c 421e fa1a 437c 433e f43a
e87a d0fa 4178 a0be 413c 411e 41be 40bc 409e 2bc0 95f0 cafc 29e0 94f8 ca7e 28f0 947c 2878 943e 283c
f968 2df0 96fc f964 2cf8 967e f962 2c7c 2c3e f2e8 f976 2efc f2e4 2e7e f2e2 e5e8 f2f6 e5e4 e5e2 cbe8
e5f6 cbe4 cbe2 97e8 cbf6 97e4 97e2 b5e0 daf8 ed7e 69c0 b4f0 da7c 68e0 b478 da3e 6870 b43c 6838 b41e
681c 25e0 92f8 c97e 6de0 24f0 927c 6cf0 b67c 923e 6c78 243c 6c3c 241e 6c1e f934 26f8 937e fb74 f932
6ef8 267c fb72 6e7c 263e 6e3e f274 277e f6f4 f272 6f7e f6f2 e4f4 edf4 e4f2 edf2 c9f4 dbf4 c9f2 dbf2
93f4 93f2 65c0 b2f0 d97c 64e0 b278 d93e 6470 b23c 6438 b21e 641c 640e 22f0 917c 66f0 2278 913e 6678
b33e 663c 221e 661e f91a 237c fb3a 677c 233e 673e f23a f67a e47a ecfa c8fa d9fa 91fa 62e0 b178 d8be
6270 b13c 6238 b11e 621c 620e 2178 90be 6378 213c 633c 211e 631e 21be 63be 6170 b0bc 6138 b09e 611c
610e 20bc 61bc 209e 619e 60b8 b05e 609c 608e 205e 60de 605c 604e 15e0 8af8 c57e 14f0 8a7c 1478 8a3e
143c 141e f8b4 16f8 8b7e f8b2 167c 163e f174 177e f172 e2f4 e2f2 c5f4 c5f2 8bf4 8bf2 35c0 9af0 cd7c
34e0 9a78 cd3e 3470 9a3c 3438 9a1e 341c 340e 12f0 897c 36f0 1278 893e 3678 9b3e 363c 121e 361e f89a
137c f9ba 377c 133e 373e f13a f37a e27a e6fa c4fa cdfa 89fa bae0 dd78 eebe 74c0 ba70 dd3c 7460 ba38
dd1e 7430 ba1c 7418 ba0e 740c 32e0 9978 ccbe 76e0 3270 993c 7670 bb3c 991e 7638 321c 761c 320e 760e
1178 88be 3378 113c 7778 333c 111e 773c 331e 771e 11be 33be 77be 72c0 b970 dcbc 7260 b938 dc9e 7230
b91c 7218 b90e 720c 7206 3170 98bc 7370 3138 989e 7338 b99e 731c 310e 730e 10bc 31bc 109e 73bc 319e
739e 7160 b8b8 dc5e 7130 b89c 7118 b88e 710c 7106 30b8 985e 71b8 309c 719c 308e 718e 105e 30de 71de
70b0 b85c 7098 b84e 708c 7086 305c 70dc 304e 70ce 7058 b82e 704c 7046 302e 706e 702c 7026 0af0 857c
0a78 853e 0a3c 0a1e 0b7c 0b3e f0ba e17a c2fa 85fa 1ae0 8d78 c6be 1a70 8d3c 1a38 8d1e 1a1c 1a0e 0978
84be 1b78 093c 1b3c 091e 1b1e 09be 1bbe 3ac0 9d70 cebc 3a60 9d38 ce9e 3a30 9d1c 3a18 9d0e 3a0c 3a06
1970 8cbc 3b70 1938 8c9e 3b38 191c 3b1c 190e 3b0e 08bc 19bc 089e 3bbc 199e 3b9e bd60 deb8 ef5e 7a40
bd30 de9c 7a20 bd18 de8e 7a10 bd0c 7a08 bd06 7a04 3960 9cb8 ce5e 7b60 3930 9c9c 7b30 bd9c 9c8e 7b18
390c 7b0c 3906 7b06 18b8 8c5e 39b8 189c 7bb8 399c 188e 7b9c 398e 7b8e 085e 18de 39de 7bde 7940 bcb0
de5c 7920 bc98 de4e 7910 bc8c 7908 bc86 7904 7902 38b0 9c5c 79b0 3898 9c4e 7998 bcce 798c 3886 7986
185c 38dc 184e 79dc 38ce 79ce 78a0 bc58 de2e 7890 bc4c 7888 bc46 7884 7882 3858 9c2e 78d8 384c 78cc
3846 78c6 182e 386e 78ee 7850 bc2c 7848 bc26 7844 7842 382c 786c 3826 7866 7828 bc16 7824 7822 3816
7836 0578 82be 053c 051e 05be 0d70 86bc 0d38 869e 0d1c 0d0e 04bc 0dbc 049e 0d9e 1d60 8eb8 c75e 1d30
8e9c 1d18 8e8e 1d0c 1d06 0cb8 865e 1db8 0c9c 1d9c 0c8e 1d8e 045e 0cde 1dde 3d40 9eb0 cf5c 3d20 9e98
cf4e 3d10 9e8c 3d08 9e86 3d04 3d02 1cb0 8e5c 3db0 1c98 8e4e 3d98 9ece 3d8c 1c86 3d86 0c5c 1cdc 0c4e
3ddc 1cce 3dce bea0 df58 efae be90 df4c be88 df46 be84 be82 3ca0 9e58 cf2e 7da0 3c90 9e4c 7d90 becc
9e46 7d88 3c84 7d84 3c82 7d82 1c58 8e2e 3cd8 1c4c 7dd8 3ccc 1c46 7dcc 3cc6 7dc6 0c2e 1c6e 3cee 7dee
be50 df2c be48 df26 be44 be42 3c50 9e2c 7cd0 3c48 9e26 7cc8 be66 7cc4 3c42 7cc2 1c2c 3c6c 1c26 7cec
3c66 7ce6 be28 df16 be24 be22 3c28 9e16 7c68 3c24 7c64 3c22 7c62 1c16 3c36 7c76 be14 be12 3c14 7c34
3c12 7c32 02bc 029e 06b8 835e 069c 068e 025e 06de 0eb0 875c 0e98 874e 0e8c 0e86 065c 0edc 064e 0ece
1ea0 8f58 c7ae 1e90 8f4c 1e88 8f46 1e84 1e82 0e58 872e 1ed8 8f6e 1ecc 0e46 1ec6 062e 0e6e 1eee 9f50
cfac 9f48 cfa6 9f44 9f42 1e50 8f2c 3ed0 9f6c 8f26 3ec8 1e44 3ec4 1e42 3ec2 0e2c 1e6c 0e26 3eec 1e66
3ee6 dfa8 efd6 dfa4 dfa2 9f28 cf96 bf68 9f24 bf64 9f22 bf62 1e28 8f16 3e68 1e24 7ee8 3e64 1e22 7ee4
3e62 7ee2 0e16 1e36 3e76 7ef6 df94 df92 9f14 bf34 9f12 bf32 1e14 3e34 1e12 7e74 3e32 7e72 df8a 9f0a
bf1a 1e0a 3e1a 7e3a 035c 034e 0758 83ae 074c 0746 032e 076e 0f50 87ac 0f48 87a6 0f44 0f42 072c 0f6c
0726 0f66 8fa8 c7d6 8fa4 8fa2 0f28 8796 1f68 8fb6 1f64 0f22 1f62 0716 0f36 1f76 cfd4 cfd2 8f94 9fb4
8f92 9fb2 0f14 1f34 0f12 3f74 1f32 3f72 cfca 8f8a 9f9a 0f0a 1f1a 3f3a 03ac 03a6 07a8 83d6 07a4 07a2
0396 07b6 87d4 87d2 0794 0fb4 0792 0fb2 c7ea
""",
)
