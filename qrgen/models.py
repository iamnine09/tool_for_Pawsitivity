import mongoengine as me
import datetime
from django.db import models
from django.conf import settings

PAPER_SIZES = ('A4', 'A3', 'A2')

# MongoDB Models (if MongoDB is available)
class QRBatch(me.Document):
    logo = me.FileField(required=True)  # Stores logo in MongoDB GridFS
    paper_size = me.StringField(choices=PAPER_SIZES, required=True)
    block_width_mm = me.FloatField(required=True)   # NEW: Width of QR+logo block
    block_height_mm = me.FloatField(required=True)  # NEW: Height of QR+logo block
    spacing_mm = me.FloatField(required=True, default=5)
    created_at = me.DateTimeField(default=datetime.datetime.utcnow)
    generated_pdf = me.FileField()  # Or use GridFS if using MongoEngine

class QRCode(me.Document):
    batch = me.ReferenceField(QRBatch, reverse_delete_rule=me.CASCADE, required=True)
    qr_image = me.FileField(required=True)

# Django Models (fallback when MongoDB is not available)
class QRBatchDjango(models.Model):
    logo = models.FileField(upload_to='logos/')
    paper_size = models.CharField(max_length=10, choices=[(size, size) for size in PAPER_SIZES])
    block_width_mm = models.FloatField()
    block_height_mm = models.FloatField()
    spacing_mm = models.FloatField(default=5)
    created_at = models.DateTimeField(auto_now_add=True)
    generated_pdf = models.FileField(upload_to='pdfs/', blank=True, null=True)

    class Meta:
        db_table = 'qrgen_qrbatch'

class QRCodeDjango(models.Model):
    batch = models.ForeignKey(QRBatchDjango, on_delete=models.CASCADE)
    qr_image = models.FileField(upload_to='qr_codes/')
    
    class Meta:
        db_table = 'qrgen_qrcode'