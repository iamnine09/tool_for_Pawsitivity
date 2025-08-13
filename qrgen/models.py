import mongoengine as me
import datetime

PAPER_SIZES = ('A4', 'A3', 'A2')

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