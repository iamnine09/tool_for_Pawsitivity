import datetime
import logging
import os
try:
    import cairosvg
    CAIRO_AVAILABLE = True
except ImportError:
    CAIRO_AVAILABLE = False
    print("Warning: cairosvg not available, PDF features will be limited")
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
from functools import partial

# Initialize process pool at module level for Windows compatibility
def initialize_process_pool():
    if os.name == 'nt':  # Windows
        multiprocessing.set_start_method('spawn', force=True)
    else:
        multiprocessing.set_start_method('fork', force=True)

try:
    initialize_process_pool()
except RuntimeError:
    # Process pool already initialized
    pass

USE_MONGODB = None
ObjectId = None

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
        # Import models here to avoid multiprocessing import issues
        try:
            from .models import QRBatch, QRCode
        except ImportError:
            from .models import QRBatchDjango as QRBatch, QRCodeDjango as QRCode

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


def process_qr_block(img_str, qr_width, qr_height, logo_width, logo_height, block_w, block_h, spacing_between_qr_logo, final_logo_bytes):
    """Process a single QR code block"""
    try:
        # Detect if the input is SVG
        is_svg = img_str.startswith('<?xml') or img_str.startswith('<svg')
        
        if is_svg:
            # Handle SVG input
            if CAIRO_AVAILABLE:
                img_data = cairosvg.svg2png(bytestring=img_str.encode('utf-8'), 
                                          output_width=int(qr_width * 4), 
                                          output_height=int(qr_height * 4), 
                                          dpi=1200)
            else:
                # Fallback: generate PNG QR code instead
                import qrcode
                qr = qrcode.QRCode(version=1, box_size=10, border=4)
                qr.add_data("fallback")
                qr.make(fit=True)
                qr_pil = qr.make_image(fill_color="black", back_color="white")
                buffer = BytesIO()
                qr_pil.save(buffer, format='PNG')
                img_data = buffer.getvalue()
        else:
            # Handle binary input
            img_data = img_str.encode('latin1')
            
        # Open and process QR code efficiently
        with Image.open(BytesIO(img_data)) as qr_img:
            qr_img = qr_img.convert("RGBA")
            # Use BICUBIC for better performance, still good quality
            qr_img = qr_img.resize((int(qr_width * 4), int(qr_height * 4)), 
                                 resample=Image.Resampling.BICUBIC)
            
            # Only enhance if it's not an SVG (SVGs are usually sharp already)
            if not is_svg:
                qr_img = qr_img.filter(ImageFilter.UnsharpMask(radius=0.5, 
                                                              percent=120, 
                                                              threshold=2))
                qr_img = ImageEnhance.Contrast(qr_img).enhance(1.2)
            
            # Create combined image
            combined = Image.new("RGBA", (int(block_w * 4), int(block_h * 4)), 
                               (255, 255, 255, 255))
            combined.paste(qr_img, (0, 0), qr_img)
            
            # Add logo at a fixed pixel offset (not scaled)
            with Image.open(BytesIO(final_logo_bytes)) as final_logo:
                final_logo = final_logo.convert("RGBA")
                fixed_spacing_px = 0  # Change this value for desired spacing
                combined.paste(final_logo, 
                             (int(qr_width * 4) + fixed_spacing_px, 0), 
                             final_logo)

        img_io = BytesIO()
        combined.convert("RGB").save(img_io, format='TIFF', compression='lzw', dpi=(1200, 1200))
        img_io.seek(0)
        return img_io
    except Exception:
        return None

def download_pdf(request):

    # Import models here to avoid multiprocessing import issues
    global USE_MONGODB, ObjectId
    try:
        from .models import QRBatch, QRCode
        from bson import ObjectId
        USE_MONGODB = True
    except ImportError:
        from .models import QRBatchDjango as QRBatch, QRCodeDjango as QRCode
        USE_MONGODB = False

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

    # Get block dimensions from frontend input
    block_w = batch.block_width_mm * mm
    block_h = batch.block_height_mm * mm
    spacing_between_blocks = batch.spacing_mm * mm  # Space between blocks from frontend

    # Hardcoded space between QR and logo (not scaled)
    spacing_between_qr_logo = 5  # This value is passed but not used for scaling anymore
    
    # Calculate equal widths for QR and logo
    qr_width = logo_width = block_w / 2  # Split block exactly in half
    qr_height = logo_height = block_h

    full_block_w = block_w
    full_block_h = block_h

    blocks_per_row = max(1, int((page_size[0] + spacing_between_blocks) // (full_block_w + spacing_between_blocks)))
    x_margin = (page_size[0] - (blocks_per_row * (full_block_w + spacing_between_blocks) - spacing_between_blocks)) / 2
    x_start = x_margin
    y_start = page_size[1] - spacing_between_blocks - full_block_h

    # Process logo once and cache it
    logo_stream = BytesIO(batch.logo.read())
    logo_original = Image.open(logo_stream).convert("RGBA")
    logo_aspect = logo_original.width / logo_original.height
    target_logo_width_px = int(logo_width * 4)
    target_logo_height_px = int(logo_height * 4)

    # Calculate dimensions maintaining aspect ratio
    if logo_aspect >= 1:
        new_logo_width = target_logo_width_px
        new_logo_height = int(target_logo_width_px / logo_aspect)
    else:
        new_logo_height = target_logo_height_px
        new_logo_width = int(target_logo_height_px * logo_aspect)

    # Resize and enhance logo once
    logo_resized = logo_original.resize((new_logo_width, new_logo_height), 
                                      resample=Image.Resampling.BICUBIC)  # BICUBIC is faster than LANCZOS
    # Only apply UnsharpMask if really needed
    if new_logo_width < logo_original.width:  # Only sharpen if we downsized
        logo_resized = logo_resized.filter(ImageFilter.UnsharpMask(radius=1, percent=150, threshold=3))
    
    # Create final logo with padding
    final_logo = Image.new("RGBA", (target_logo_width_px, target_logo_height_px), (255, 255, 255, 255))
    final_logo.paste(logo_resized, 
                    ((target_logo_width_px - new_logo_width) // 2, 
                     (target_logo_height_px - new_logo_height) // 2), 
                    logo_resized)

    # Cache logo bytes
    final_logo_bytes = BytesIO()
    final_logo.save(final_logo_bytes, format='PNG', optimize=True)
    final_logo_bytes = final_logo_bytes.getvalue()
    
    # Clean up original images to free memory
    logo_original.close()
    logo_resized.close()
    logo_stream.close()

    # Prepare args for multiprocessing
    tasks = [
        (img_str, qr_width, qr_height, logo_width, logo_height, block_w, block_h, spacing_between_qr_logo, final_logo_bytes)
        for img_str in qr_data_list
    ]

    # Calculate optimal batch size based on CPU cores
    num_cores = multiprocessing.cpu_count()
    batch_size = max(4, num_cores * 2)  # Use more batches for more cores
    row = 0
    col = 0
    x = x_start
    y = y_start

    def process_batch_sequential(batch_tasks):
        """Process tasks sequentially as fallback"""
        return [process_qr_block(*task) for task in batch_tasks]

    def process_batch_parallel(batch_tasks, executor):
        """Process tasks in parallel with timeout"""
        futures = []
        for task in batch_tasks:
            # Unpack task tuple into separate arguments
            future = executor.submit(process_qr_block, *task)
            futures.append(future)
            
        results = []
        for future in futures:
            try:
                result = future.result(timeout=30)  # 30 second timeout per task
                results.append(result)
            except Exception as e:
                logging.error(f"Error processing QR batch: {str(e)}")
                results.append(None)
        return results

    parallel_failed = False
    try:
        # Try parallel processing first
        with ProcessPoolExecutor(max_workers=num_cores) as executor:
            for i in range(0, len(tasks), batch_size):
                batch_tasks = tasks[i:i + batch_size]
                try:
                    batch_results = process_batch_parallel(batch_tasks, executor)
                except Exception as e:
                    logging.error(f"Parallel processing failed: {str(e)}")
                    parallel_failed = True
                    break

                for img_io in batch_results:
                    if img_io is None:
                        continue
                    
                    try:
                        # Calculate position
                        x = x_start + col * (full_block_w + spacing_between_blocks)
                        y = y_start - row * (full_block_h + spacing_between_blocks)
                        
                        # Start new page if needed
                        if y < spacing_between_blocks:
                            c.showPage()
                            row = 0
                            col = 0
                            x = x_start
                            y = y_start
                        
                        # Draw image and update position
                        c.drawImage(ImageReader(img_io), x, y, full_block_w, full_block_h, mask='auto')
                        
                        col = (col + 1) % blocks_per_row
                        if col == 0:
                            row += 1
                    except Exception as e:
                        logging.error(f"Error drawing QR code: {str(e)}")
                    finally:
                        # Always clean up
                        try:
                            img_io.close()
                        except:
                            pass  # Ignore cleanup errors
    except Exception as e:
        logging.error(f"Process pool error: {str(e)}")
        parallel_failed = True

    # If parallel processing failed, fall back to sequential
    if parallel_failed:
        logging.info("Falling back to sequential processing")
        for i in range(0, len(tasks), batch_size):
            batch_tasks = tasks[i:i + batch_size]
            batch_results = process_batch_sequential(batch_tasks)
            
            for img_io in batch_results:
                if img_io is None:
                    continue
                
                # Calculate position
                x = x_start + col * (full_block_w + spacing_between_blocks)
                y = y_start - row * (full_block_h + spacing_between_blocks)
                
                # Start new page if needed
                if y < spacing_between_blocks:
                    c.showPage()
                    row = 0
                    col = 0
                    x = x_start
                    y = y_start
                
                # Draw image and update position
                c.drawImage(ImageReader(img_io), x, y, full_block_w, full_block_h, mask='auto')
                img_io.close()  # Clean up memory
                
                col = (col + 1) % blocks_per_row
                if col == 0:
                    row += 1

    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    response.write(pdf)
    return response
