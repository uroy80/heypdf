# Bundled fonts

Used for text heypdf adds to PDFs (Edit, Watermark, Page numbers, typed signatures, stamps) and for SVG rendering
(images → PDF). Licence texts are in `licenses/`.

| file | family | licence | source |
|---|---|---|---|
| Anton-Regular.ttf | Anton | OFL-1.1 (`licenses/OFL-Anton.txt`) | google/fonts `ofl/anton` |
| LibreFranklin-Variable.ttf | Libre Franklin (wght 100–900) | OFL-1.1 (`licenses/OFL-LibreFranklin.txt`) | google/fonts `ofl/librefranklin/LibreFranklin[wght].ttf` |
| Fraunces-Variable.ttf | Fraunces (opsz, wght, SOFT, WONK) | OFL-1.1 (`licenses/OFL-Fraunces.txt`) | google/fonts `ofl/fraunces/Fraunces[SOFT,WONK,opsz,wght].ttf` |
| IBMPlexMono-Regular.ttf, IBMPlexMono-Bold.ttf | IBM Plex Mono | OFL-1.1 (`licenses/OFL-IBMPlexMono.txt`) | google/fonts `ofl/ibmplexmono` |
| Caveat-Variable.ttf | Caveat (wght 400–700) | OFL-1.1 (`licenses/OFL-Caveat.txt`) | google/fonts `ofl/caveat/Caveat[wght].ttf` |
| DancingScript-Variable.ttf | Dancing Script (wght 400–700) | OFL-1.1 (`licenses/OFL-DancingScript.txt`) | google/fonts `ofl/dancingscript/DancingScript[wght].ttf` |
| GreatVibes-Regular.ttf | Great Vibes | OFL-1.1 (`licenses/OFL-GreatVibes.txt`) | google/fonts `ofl/greatvibes` |
| HomemadeApple-Regular.ttf | Homemade Apple | Apache-2.0 (`licenses/LICENSE-HomemadeApple.txt`) | google/fonts `apache/homemadeapple` |
| DejaVuSans.ttf, DejaVuSans-Bold.ttf, DejaVuSerif.ttf, DejaVuSansMono.ttf | DejaVu | Bitstream Vera / Arev licence + public-domain changes (`licenses/LICENSE-DejaVu.txt`) | github.com/dejavu-fonts/dejavu-fonts release `version_2_37`, `dejavu-fonts-ttf-2.37.tar.bz2` (sha256 `fa9ca4d13871dd122f61258a80d01751d603b4d3ee14095d65453b4e846e17d7`) |
| NotoSansDevanagari-Regular.ttf, NotoSansDevanagari-Bold.ttf, NotoSansBengali-*, NotoSansGurmukhi-*, NotoSansGujarati-*, NotoSansOriya-*, NotoSansTamil-*, NotoSansTelugu-*, NotoSansKannada-*, NotoSansMalayalam-*, NotoSansHebrew-*, NotoSansThai-* (Regular + Bold), NotoNaskhArabic-Regular.ttf, NotoNaskhArabic-Bold.ttf | Noto (fallback faces for Indic scripts, Hebrew, Thai, Arabic) | OFL-1.1 (`licenses/OFL-Noto.txt`) | Debian `fonts-noto-core` 20201225-2 (`/usr/share/fonts/truetype/noto/`), files unmodified |
| NotoSansCJKsc-Subset.otf | Noto Sans CJK SC (fallback for Chinese, Japanese, Korean) | OFL-1.1 (`licenses/OFL-NotoSansCJK.txt`) | Debian `fonts-noto-cjk` 1:20240730+repack1-1 `NotoSansCJK-Regular.ttc` face 2 (SC, v2.004), subset with fontTools (see below) |

The Noto faces are the per-character fallback chain of `app/imaging/fonts.py` (`FALLBACKS`) and are loaded by
resvg for SVG text. Every file named in `FALLBACKS` must be listed here. NotoSansCJKsc-Subset.otf keeps
GB2312, JIS X 0208, Big5 level 1, the 2 350 KS X 1001 Hangul syllables, kana, CJK punctuation and full-width forms
(14 882 code points, ccmp/kern/liga/calt/mark/mkmk features, no hinting; the reserved font name "Source" is not
used): `fontTools.subset` over face 2 of the Debian TTC (code points from Python's gb2312, euc_jp, euc_kr and big5
codecs plus the ranges above; `hinting=False`, `layout_features=[ccmp, kern, liga, calt, mark, mkmk]`).

google/fonts files were fetched from commit `47d54ca1ebb14fd864695f1d39dc734f39017c33`
(`https://raw.githubusercontent.com/google/fonts/<commit>/<path>`); the variable fonts were renamed
(`[axes]` → `-Variable`), their content is unmodified.

sha256 of the shipped files:

```
a4ba3a92350ebb031da0cb47630ac49eb265082ca1bc0450442f4a83ab947cab  Anton-Regular.ttf
0bdb6b660482d31531b3945849fba5916b3ef8695da7024a9e6b9ee3c4157988  Caveat-Variable.ttf
21808625578fe8d8cd10cb684be546dca077b27cd03a53a2f1ec11dc743c924c  DancingScript-Variable.ttf
e6476c1b80502924294eed40894c5b18e06c181444ca953e5334262df9c27724  DejaVuSans-Bold.ttf
7da195a74c55bef988d0d48f9508bd5d849425c1770dba5d7bfc6ce9ed848954  DejaVuSans.ttf
b4a6c3e4faab8773f4ff761d56451646409f29abedd68f05d38c2df667d3c582  DejaVuSansMono.ttf
42d1edeb7952f31b1f96d767ed7030b08a39e0c372b0071641518864e2bffb51  DejaVuSerif.ttf
177ff6c0f14e5550a3c624247cd1189611d4eb65d000b14944c63d967958abbb  Fraunces-Variable.ttf
8d509802186f1b51572531ecf313e8098f9a5bfdfaca93f0c9b34467f9982d15  GreatVibes-Regular.ttf
dd1baaca3cde1b1f8415aed3b0aea6808655c9d9ca5a99c7282d9accc16c1a58  HomemadeApple-Regular.ttf
ac27abd6450a64dd94467580a02fe6235156d5b92f2926ebbc8e7489df64e0be  IBMPlexMono-Bold.ttf
6a3412f058c7d8dfd9170c41e85ade48e5156ecb89356110ca57a0a27734af46  IBMPlexMono-Regular.ttf
2329f394b10ed1c71107df20fddc11e2bb1b68c1ecbb385f975b36fd64a971f8  LibreFranklin-Variable.ttf
b5cbcb561b4b540ff11d7b6934577dbbbbb9e1d152d47f6f31f19176d58ff39c  NotoNaskhArabic-Bold.ttf
8c67eaa2f0086872628fc3f7f760386f83c8be5c5e9a1ba3a38891237b2ac7cb  NotoNaskhArabic-Regular.ttf
22c9ff99baca4d31c531ce450a1369beed889467e1ad964f4b8a6f810aa46955  NotoSansBengali-Bold.ttf
9bb35b3547829ecc42041b6017e87fd4c5f1da1edcd00f59171777050f346018  NotoSansBengali-Regular.ttf
77ad688cfe1dc55185bf4edb4db3c614202c65dd23a1463ad0e23fc52bcc9a5c  NotoSansDevanagari-Bold.ttf
79a470365ccb210fa3c7d8d8ff2e005ef9d983cfd067f735a0caf7e15070ca9f  NotoSansDevanagari-Regular.ttf
db2b642c28b021da8cb0f3463bf41376549c850aa718ca863ec3ff5d5df0f2f7  NotoSansGujarati-Bold.ttf
4468a3a24516831874fb4a867b760a22257944584e478d32a97e3c4d4bfc6400  NotoSansGujarati-Regular.ttf
ded9c1f56a7ed0b835cfef5dd642f26cfc0f41a16fbc08008d376ddcc91d606e  NotoSansGurmukhi-Bold.ttf
1db960ff6f506a98f5b3ece78f54618388c6187e7edf8ceee5ab63389c0db3ab  NotoSansGurmukhi-Regular.ttf
d3b01343f71e8a93fffb84c345a1aecbaf4694b98d0eaace916cb9eac9e6dcd3  NotoSansHebrew-Bold.ttf
436900d5ad77d33e4234247f3076eaecd25b92b9bea0519514f684140e3566a7  NotoSansHebrew-Regular.ttf
fcb9918669a24e33b1464b33eeac24e9a0f54a50393dfdaeb1e596cfcf2dac47  NotoSansKannada-Bold.ttf
892b841b0b87ca3c2702bdc78d3031c8220f53e8f5f496ec5e37ccf0ca03ddf7  NotoSansKannada-Regular.ttf
abe54dff7c6539899f1208672ae4235207b3e6b8d49594e9ff02d972d5975a81  NotoSansMalayalam-Bold.ttf
5433f4a642b4325aa169a55c4c6b8d1c4a45c4e08bcd9cbe72888229709a8f2d  NotoSansMalayalam-Regular.ttf
d563e031a325aa1d5fa7975a7fed3f34409b3ef10036f75d275653e7e4bad5c2  NotoSansOriya-Bold.ttf
20751837a8cac9d0e610ffdf91c43d3391e2b267ca2cad07f6e3866798436369  NotoSansOriya-Regular.ttf
7ef7071ba2fad67dcae397dc7f5bfd4dbce69d86cdc453c2cc4abe640df0b442  NotoSansTamil-Bold.ttf
a7bb7f92f165479af2f0556c4fd669b74c5959f464f603e18389b532f4d07a71  NotoSansTamil-Regular.ttf
34e3a81c02ea8474f00b1046cbb183461648fd253a56475140d6b025db828b5b  NotoSansTelugu-Bold.ttf
cb3165071121b1e80c55a041d0c1f79b3bbc68e4b0eab37b0d881958dfc3ed7a  NotoSansTelugu-Regular.ttf
97bbc8ea98328325285f2f552228c7aac1db39d8f38f0096a61ba7df1f1f780e  NotoSansThai-Bold.ttf
dfbd5ed0f1e68c3cbc51aa08f646d1ab0e68e7c4ceab70a69e465225ff701f93  NotoSansThai-Regular.ttf
220db443ff160134ed9d85f26e14fe336a474f73ea457aca22b26d14ad870bbd  NotoSansCJKsc-Subset.otf
```
