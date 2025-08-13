import datetime
import logging
import cairosvg
import imghdr
from io import BytesIO
from PIL import Image, ImageFilter, ImageEnhance
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4, A3, A2
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from django.shortcuts import render, redirect
from django.http import HttpResponse
from django import forms
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

# Try to import MongoDB models, fall back to Django models
try:
    from .models import QRBatch, QRCode
    from bson import ObjectId
    USE_MONGODB = True
except ImportError:
    from .models import QRBatchDjango as QRBatch, QRCodeDjango as QRCode
    USE_MONGODB = False

logger = logging.getLogger(__name__)
PAPER_SIZE_MAP = {'A4': A4, 'A3': A3, 'A2': A2}

class QRBatchForm(forms.Form):
    logo = forms.ImageField(required=True)
    paper_size = forms.ChoiceField(choices=[('A4', 'A4'), ('A3', 'A3'), ('A2', 'A2')])
    block_width_mm = forms.FloatField(min_value=10, label="Block Width (mm)")
    block_height_mm = forms.FloatField(min_value=10, label="Block Height (mm)")
    spacing_mm = forms.FloatField(min_value=0, label="Spacing Between Blocks (mm)", initial=5)

def index(request):
    if request.method == 'POST':
        form = QRBatchForm(request.POST, request.FILES)
        if form.is_valid():
            logo_file = form.cleaned_data['logo']
            batch = QRBatch(
                paper_size=form.cleaned_data['paper_size'],
                block_width_mm=form.cleaned_data['block_width_mm'],
                block_height_mm=form.cleaned_data['block_height_mm'],
                spacing_mm=form.cleaned_data['spacing_mm'],
                created_at=datetime.datetime.utcnow()
            )
            batch.logo.put(logo_file, content_type=logo_file.content_type)
            batch.save()

            # Read all QR image files (no DB save)
            qr_images = request.FILES.getlist('qr_images')
            qr_data_list = []
            for img in qr_images:
                ext = img.name.split('.')[-1].lower()
                if ext not in ['png', 'jpg', 'jpeg', 'svg']:
                    continue
                if ext != 'svg' and imghdr.what(img) is None:
                    continue
                qr_data_list.append(img.read())

            request.session['batch_id'] = str(batch.id)
            request.session['qr_data_list'] = [img.decode('latin1') for img in qr_data_list]  # serialize to string
            return redirect('qrgen:download_pdf')
    else:
        form = QRBatchForm()
    return render(request, 'qrgen/index.html', {'form': form})


def process_qr_block(args):
    img_data_str, qr_width, qr_height, logo_width, logo_height, block_w, block_h, spacing_between_qr_logo, final_logo_bytes = args
    img_data = img_data_str.encode('latin1')  # deserialize

    try:
        is_svg = img_data.strip().startswith(b'<svg') or b'<svg' in img_data[:500].lower()
        if is_svg:
            img_data = cairosvg.svg2png(bytestring=img_data, output_width=int(qr_width * 4), output_height=int(qr_height * 4), dpi=1200)
        qr_img = Image.open(BytesIO(img_data)).convert("RGBA")
        qr_img = qr_img.resize((int(qr_width * 4), int(qr_height * 4)), resample=Image.Resampling.LANCZOS)
        qr_img = qr_img.filter(ImageFilter.UnsharpMask(radius=0.5, percent=120, threshold=2))
        qr_img = ImageEnhance.Contrast(qr_img).enhance(1.2)

        final_logo = Image.open(BytesIO(final_logo_bytes)).convert("RGBA")
        combined = Image.new("RGBA", (int(block_w * 4), int(block_h * 4)), (255, 255, 255, 255))
        combined.paste(qr_img, (0, 0), qr_img)
        combined.paste(final_logo, (int(qr_width * 4 + spacing_between_qr_logo * 4), 0), final_logo)

        img_io = BytesIO()
        combined.convert("RGB").save(img_io, format='TIFF', compression='lzw', dpi=(1200, 1200))
        img_io.seek(0)
        return img_io
    except Exception:
        return None

def download_pdf(request):
    batch_id = request.session.get('batch_id')
    qr_data_list_raw = request.session.get('qr_data_list')
    if not batch_id or not qr_data_list_raw:
        return HttpResponse("No batch found", status=404)

    batch = QRBatch.objects.get(id=ObjectId(batch_id))
    qr_data_list = qr_data_list_raw  # List of latin1-encoded strings

    response = HttpResponse(content_type='application/pdf')
    response['Content-Disposition'] = 'attachment; filename="qrcodes.pdf"'
    buffer = BytesIO()
    page_size = PAPER_SIZE_MAP[batch.paper_size]
    c = canvas.Canvas(buffer, pagesize=page_size)

    block_w = batch.block_width_mm * mm
    block_h = batch.block_height_mm * mm
    spacing_between_qr_logo = 1 * mm
    spacing_between_blocks = batch.spacing_mm * mm

    qr_logo_width_each = (block_w - spacing_between_qr_logo) / 2
    qr_width = logo_width = qr_logo_width_each
    qr_height = logo_height = block_h

    full_block_w = block_w
    full_block_h = block_h

    blocks_per_row = max(1, int((page_size[0] + spacing_between_blocks) // (full_block_w + spacing_between_blocks)))
    x_margin = (page_size[0] - (blocks_per_row * (full_block_w + spacing_between_blocks) - spacing_between_blocks)) / 2
    x_start = x_margin
    y_start = page_size[1] - spacing_between_blocks - full_block_h

    # Process logo
    logo_stream = BytesIO(batch.logo.read())
    logo_original = Image.open(logo_stream).convert("RGBA")
    logo_aspect = logo_original.width / logo_original.height
    target_logo_width_px = int(logo_width * 4)
    target_logo_height_px = int(logo_height * 4)

    if logo_aspect >= 1:
        new_logo_width = target_logo_width_px
        new_logo_height = int(target_logo_width_px / logo_aspect)
    else:
        new_logo_height = target_logo_height_px
        new_logo_width = int(target_logo_height_px * logo_aspect)

    logo_resized = logo_original.resize((new_logo_width, new_logo_height), resample=Image.Resampling.LANCZOS)
    logo_resized = logo_resized.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3))
    final_logo = Image.new("RGBA", (target_logo_width_px, target_logo_height_px), (255, 255, 255, 255))
    final_logo.paste(logo_resized, ((target_logo_width_px - new_logo_width) // 2, (target_logo_height_px - new_logo_height) // 2), logo_resized)

    final_logo_bytes = BytesIO()
    final_logo.save(final_logo_bytes, format='PNG')
    final_logo_bytes = final_logo_bytes.getvalue()

    # Prepare args for multiprocessing
    tasks = [
        (img_str, qr_width, qr_height, logo_width, logo_height, block_w, block_h, spacing_between_qr_logo, final_logo_bytes)
        for img_str in qr_data_list
    ]

    with ProcessPoolExecutor(max_workers=multiprocessing.cpu_count()) as executor:
        results = list(executor.map(process_qr_block, tasks))

    count = 0
    for img_io in results:
        if img_io is None:
            continue
        col = count % blocks_per_row
        row = count // blocks_per_row
        x = x_start + col * (full_block_w + spacing_between_blocks)
        y = y_start - row * (full_block_h + spacing_between_blocks)
        if y < spacing_between_blocks:
            c.showPage()
            count = 0
            x = x_start
            y = y_start
            row = 0
            col = 0
        c.drawImage(ImageReader(img_io), x, y, full_block_w, full_block_h, mask='auto')
        count += 1

    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    response.write(pdf)
    return response
