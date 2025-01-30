import os
import time
import threading
import libtorrent as lt
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)
app.config['DOWNLOAD_FOLDER'] = 'downloads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB limit for magnet links

# Global state for active downloads
downloads = {}
lock = threading.Lock()

class TorrentDownloader(threading.Thread):
    def __init__(self, magnet_link):
        super().__init__()
        self.magnet_link = magnet_link
        self.info_hash = ""
        self.progress = 0.0
        self.speed = 0
        self.status = "Initializing"
        self.files = []
        self.running = True

    def run(self):
        ses = lt.session()
        ses.listen_on(6881, 6891)
        
        params = {
            'save_path': app.config['DOWNLOAD_FOLDER'],
            'storage_mode': lt.storage_mode_t(2),
            'auto_managed': True,
        }

        handle = lt.add_magnet_uri(ses, self.magnet_link, params)
        ses.start_dht()
        
        self.info_hash = str(handle.info_hash())
        
        with lock:
            downloads[self.info_hash] = self

        print("Downloading metadata...")
        while not handle.has_metadata() and self.running:
            time.sleep(1)
        
        if not self.running:
            return

        torinfo = handle.get_torrent_info()
        self.files = [{'index': i, 'path': file.path, 'size': file.size} 
                     for i, file in enumerate(torinfo.files())]
        
        self.status = "Downloading"
        while handle.status().state != lt.torrent_status.seeding and self.running:
            s = handle.status()
            self.progress = s.progress * 100
            self.speed = s.download_rate
            time.sleep(1)

        if self.running:
            self.status = "Completed"
        else:
            self.status = "Stopped"

    def stop(self):
        self.running = False
        self.status = "Stopping"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download', methods=['POST'])
def start_download():
    magnet_link = request.form['magnet']
    if not magnet_link.startswith('magnet:'):
        return jsonify({'error': 'Invalid magnet link'}), 400
    
    downloader = TorrentDownloader(magnet_link)
    downloader.start()
    
    return jsonify({
        'id': downloader.info_hash,
        'status': downloader.status
    })

@app.route('/progress/<info_hash>')
def get_progress(info_hash):
    with lock:
        download = downloads.get(info_hash)
    
    if not download:
        return jsonify({'error': 'Download not found'}), 404
    
    return jsonify({
        'progress': download.progress,
        'speed': download.speed,
        'status': download.status,
        'files': download.files
    })

@app.route('/downloads/<path:filename>')
def download_file(filename):
    return send_from_directory(
        app.config['DOWNLOAD_FOLDER'],
        filename,
        as_attachment=True
    )

@app.route('/stop/<info_hash>')
def stop_download(info_hash):
    with lock:
        download = downloads.get(info_hash)
    
    if download:
        download.stop()
        return jsonify({'status': 'Stopping'})
    
    return jsonify({'error': 'Download not found'}), 404

if __name__ == '__main__':
    os.makedirs(app.config['DOWNLOAD_FOLDER'], exist_ok=True)
    app.run(threaded=True, debug=True)
