"""Preserve an image region from the original unrotated PDF, not an OCR redraw."""
import io
import importlib.metadata
import math
from mapping import require
from common import atomic,confined,digest


def crop_image(source,page_number,bbox,staging):
    import pypdfium2 as pdfium
    require(isinstance(bbox,list) and len(bbox)==4 and 0<=bbox[0]<bbox[2]<=1 and 0<=bbox[1]<bbox[3]<=1,
            'Cannot preserve image without a valid source region')
    document=pdfium.PdfDocument(str(source));page=None;bitmap=None
    try:
        page=document[page_number-1]
        require(page.get_rotation()==0,'Image crop rotation requires a verified transform')
        bitmap=page.render(scale=3);image=bitmap.to_pil();w,h=image.size
        crop=image.crop((math.floor(bbox[0]*w),math.floor(bbox[1]*h),math.ceil(bbox[2]*w),math.ceil(bbox[3]*h)))
        buf=io.BytesIO();crop.save(buf,format='PNG');data=buf.getvalue();path='assets/'+digest(data)+'.png'
        atomic(confined(staging,path),data)
        return {'path':path,'sha256':digest(data),'pdf_page':page_number,'bbox':bbox,
                'method':'original_pdf_region_render','renderer':'pypdfium2','renderer_version':importlib.metadata.version('pypdfium2'),
                'scale':3,'verification':'approximate','limitation':'Region follows engine bbox; original pixels rendered, not original embedded image bytes.'}
    finally:
        if bitmap:bitmap.close()
        if page:page.close()
        document.close()
