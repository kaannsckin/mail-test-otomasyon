import os
import email
import traceback
from receiver import BaseReceiver

class DummyReceiver(BaseReceiver):
    def wait_for_message(self, *args, **kwargs):
        pass

rec = DummyReceiver({})

folder = '/Users/kaan/Downloads/test_eml'
for f in sorted(os.listdir(folder)):
    if not f.endswith('.eml'): continue
    path = os.path.join(folder, f)
    with open(path, 'rb') as file:
        raw = file.read()
    
    msg = email.message_from_bytes(raw)
    try:
        import json
        details = rec._extract_details(msg, raw, f)
        print(f"[{f}] is_multipart={details['is_multipart']}")
        
        def _safe_json(obj):
            if isinstance(obj, bytes):
                return f"<bytes len={len(obj)}>"
            if isinstance(obj, dict):
                return {k: _safe_json(v) for k, v in obj.items() if k != "data"}
            if isinstance(obj, list):
                return [_safe_json(i) for i in obj]
            return obj

        try:
            print("  dumping headers...")
            json.dumps(_safe_json(details["headers"]))
            print("  dumping parts...")
            json.dumps(_safe_json(details["parts"][:5]))
            print("  dumping attachments...")
            json.dumps(_safe_json(details["attachments"]))
        except Exception as jej:
            print(f"  JSON DUMP ERROR: {jej}")

        html_part = next((p for p in details["parts"] if p["content_type"] == "text/html"), None)
        if html_part:
            print(f"  found html_part len={len(html_part['full_text'])}")
        else:
            print("  NO html_part found!")
    except Exception as e:
        print(f"Error parsing {f}: {e}")
        traceback.print_exc()
