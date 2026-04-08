import re

with open('templates/index.html', 'r', encoding='utf-8') as f:
    html = f.read()

# Add Checkbox in 🚀 Çalıştır Card
checkbox_html = '''
            <div class="form-group" style="grid-column: 1 / -1;">
              <label class="flex-center font-size-13" style="gap:8px; cursor:pointer;">
                <input type="checkbox" id="saveToSent">
                <span>Gönderilenleri Sent Klasörüne Kaydet (X-Mailer Ekle)</span>
              </label>
            </div>
          </div>
          <div id="analyzerModeInfo"'''

html = html.replace('          </div>\n          <div id="analyzerModeInfo"', checkbox_html)

# Add to startRun API Payload
js_target = "  if (selectedSpoofClient) body.spoof_sender = selectedSpoofClient;"
js_replace = js_target + "\n  body.save_to_sent = document.getElementById('saveToSent').checked;"
html = html.replace(js_target, js_replace)

with open('templates/index.html', 'w', encoding='utf-8') as f:
    f.write(html)
print("index.html patched")
