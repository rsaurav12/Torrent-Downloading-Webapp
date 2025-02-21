import libtorrent as lt 
import time
import os
import shutil
import logging
import threading
import zipfile
import json
from flask import Flask, request, render_template, jsonify, send_from_directory
from logging.handlers import RotatingFileHandler

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SESSION_TYPE'] = 'filesystem'

# Configure logging
handler = RotatingFileHandler('app.log', maxBytes=10000, backupCount=3)
handler.setLevel(logging.DEBUG)
app.logger.addHandler(handler)
logging.basicConfig(level=logging.DEBUG)

# Directory for JSON session storage
SESSION_DIR = 'session_store'
if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

def save_session(session_id, data):
    path = os.path.join(SESSION_DIR, session_id + '.json')
    with open(path, 'w') as f:
        json.dump(data, f)

def load_session(session_id):
    path = os.path.join(SESSION_DIR, session_id + '.json')
    if not os.path.exists(path):
        return None
    with open(path, 'r') as f:
        return json.load(f)

def convert_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def clean_ghost_files(directory):
    if os.path.exists(directory):
        shutil.rmtree(directory)
    os.makedirs(directory)
def zip_directory(session_id, source_dir, output_filename):
    try:
        # Ensure that the destination directory for the zip exists
        dest_dir = os.path.dirname(output_filename)
        os.makedirs(dest_dir, exist_ok=True)
        
        # Check that the source directory exists before attempting to zip it
        if not os.path.exists(source_dir):
            raise Exception(f"Source directory '{source_dir}' does not exist")
        
        # Create the zip file
        with zipfile.ZipFile(output_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, start=source_dir)
                    zipf.write(file_path, arcname)
        
        # Update the session to indicate completion
        session = load_session(session_id) or {}
        session['zip_path'] = output_filename
        session['status'] = 'Completed'
        save_session(session_id, session)
        
        # Remove the source directory (if desired)
        shutil.rmtree(source_dir)
        app.logger.info(f"Zipping complete for session {session_id}")
    except Exception as e:
        app.logger.error(f"Error zipping files for session {session_id}: {str(e)}")
        session = load_session(session_id) or {}
        session['status'] = f'Error: {str(e)}'
        save_session(session_id, session)

def fetch_metadata(session_id, magnet_link, save_path):
    try:
        app.logger.debug(f"Fetching metadata for session {session_id} with magnet {magnet_link}")
        ses = lt.session()
        atp = lt.parse_magnet_uri(magnet_link)
        atp.save_path = save_path  # Ensure the save_path is set
        handle = ses.add_torrent(atp)
        session = load_session(session_id) or {}
        session['status'] = 'Fetching metadata...'
        session['handle'] = None  # We cannot serialize the handle; store a flag instead if needed.
        save_session(session_id, session)
        while not handle.has_metadata():
            time.sleep(1)
        torinfo = handle.get_torrent_info()
        files = torinfo.files()
        file_list = []
        for i in range(files.num_files()):
            file_list.append({
                'index': i,
                'path': files.file_path(i),
                'size': files.file_size(i),
                'size_str': convert_bytes(files.file_size(i))
            })
        session = load_session(session_id) or {}
        session.update({
            'files': file_list,
            'status': 'Metadata fetched',
            'save_path': save_path,
            'total_size': sum(files.file_size(i) for i in range(files.num_files()))
        })
        # Note: We do not store the torrent handle here (not serializable).
        save_session(session_id, session)
        app.logger.info(f"Metadata fetched for session {session_id}")
    except Exception as e:
        app.logger.error(f"Metadata fetch error for session {session_id}: {str(e)}")
        session = load_session(session_id) or {}
        session['status'] = f'Error: {str(e)}'
        save_session(session_id, session)

def download_files(session_id, selected_indices):
    try:
        app.logger.debug(f"Starting download for session {session_id} with file indices: {selected_indices}")
        session = load_session(session_id)
        if session is None:
            raise Exception("Session not found")
        # Recreate the session using libtorrent by assuming the metadata is already fetched.
        # Note: The torrent handle is not persisted across workers, so here we reinitialize a new session.
        ses = lt.session()
        atp = lt.parse_magnet_uri(session['magnet'])
        atp.save_path = session['save_path']
        handle = ses.add_torrent(atp)
        # Wait for metadata (should be instantaneous since we already fetched it)
        while not handle.has_metadata():
            time.sleep(1)
        files = handle.get_torrent_info().files()
        os.makedirs(session['save_path'], exist_ok=True)
        clean_ghost_files(session['save_path'])
        priorities = [1 if i in selected_indices else 0 for i in range(files.num_files())]
        handle.prioritize_files(priorities)
        handle.resume()
        session['status'] = 'Downloading'
        session['progress'] = 0
        session['download_speed'] = 0
        session['downloaded'] = 0
        session['selected_size'] = sum(files.file_size(i) for i in selected_indices)
        save_session(session_id, session)
    
        '''if status.progress >= 1.0 and status.state == lt.torrent_status.finished:
            break  # Explicitly exit if all pieces are downloaded'''
        
        while handle.status().state != 'seeding' and handle.status().state != 'finished' and handle.status().progress<1:
            status = handle.status()
            session = load_session(session_id) or {}
            session.update({
                'progress': status.progress * 100,
                'download_speed': status.download_rate,
                'downloaded': status.total_done,
                'status': f"{status.state} - {str(status.progress * 100)}%"
            })
            
            save_session(session_id, session)
            time.sleep(1)
        handle.flush_cache()
        handle.pause()
        session = load_session(session_id) or {}
        session.update({
            'progress': 100,
            'status': "Downloaded. Zipping your files."
        })
         
        save_session(session_id, session)
        zip_filename = os.path.join('zips', f'{session_id}.zip')
        threading.Thread(target=zip_directory, args=(session_id, session['save_path'], zip_filename)).start()
        app.logger.info(f"Download complete for session {session_id}. Zipping initiated.")
    except Exception as e:
        app.logger.error(f"Download error for session {session_id}: {str(e)}")
        session = load_session(session_id) or {}
        session['status'] = f'Error: {str(e)}'
        save_session(session_id, session)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/submit', methods=['POST'])
def submit():
    magnet_link = request.form['magnet']
    folder_name = request.form['folder']
    app.logger.info(f"Received submit: magnet={magnet_link}, folder={folder_name}")
    session_id = os.urandom(16).hex()
    save_path = os.path.join('downloads', folder_name)
    os.makedirs(save_path, exist_ok=True)
    session_data = {
        'status': 'Initializing...',
        'save_path': save_path,
        'magnet': magnet_link
    }
    save_session(session_id, session_data)
    threading.Thread(target=fetch_metadata, args=(session_id, magnet_link, save_path)).start()
    return jsonify({'session_id': session_id})

@app.route('/start-download', methods=['POST'])
def start_download():
    session_id = request.form['session_id']
    selected = request.form.getlist('files')
    app.logger.info(f"Starting download for session {session_id} with selected files: {selected}")
    try:
        selected_indices = [int(idx) for idx in selected]
    except Exception as e:
        app.logger.error(f"Error converting selected files: {str(e)}")
        return jsonify({'error': str(e)}), 400
    session = load_session(session_id)
    if session is None:
        return jsonify({'error': 'Session not found'}), 404
    session['selected_indices'] = selected_indices
    save_session(session_id, session)
    threading.Thread(target=download_files, args=(session_id, selected_indices)).start()
    return jsonify({'session_id': session_id})

@app.route('/progress/<session_id>')
def progress(session_id):
    return render_template('progress.html', session_id=session_id)

@app.route('/status/<session_id>')
def get_status(session_id):
    session = load_session(session_id) or {}
    return jsonify({
        'status': session.get('status', 'Unknown'),
        'progress': session.get('progress', 0),
        'download_speed': f"{convert_bytes(session.get('download_speed', 0))}/s",
        'downloaded': convert_bytes(session.get('downloaded', 0)),
        'total_size': convert_bytes(session.get('selected_size', 0)),
        'zip_ready': 'zip_path' in session,
        'files': session.get('files', [])
    })

@app.route('/download/<session_id>')
def download_zip(session_id):
    session = load_session(session_id)
    if not session or 'zip_path' not in session:
        return "File not ready", 404
    return send_from_directory('zips', f'{session_id}.zip', as_attachment=True)

if __name__ == '__main__':
    os.makedirs('downloads', exist_ok=True)
    os.makedirs('zips', exist_ok=True)
    app.run(threaded=True)
