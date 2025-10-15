import json
import os
import re
from collections import defaultdict

def format_seconds_to_hhmmss(seconds: float) -> str:
    """Converts a float of total seconds into HH:MM:SS format."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"{hours:02}:{minutes:02}:{secs:02}"

def parse_whisperx_transcript(transcript_text: str) -> list:
    """
    Parses the STT output text into a structured list of segments.
    """
    segments = []
    # This regex handles multiline text within a single speaker segment
    pattern = re.compile(
        r'\[(SPEAKER_\w+)\]\s'      # Speaker ID (e.g., [SPEAKER_00])
        r'\[([\d.]+) - ([\d.]+)\]\n' # Timestamp (e.g., [6.75 - 22.56])
        r'(.+?)\n\n',               # The actual text, non-greedy
        re.DOTALL                   # . matches newline
    )
    matches = pattern.findall(transcript_text)
    
    for match in matches:
        speaker_id, start_time, end_time, text = match
        segments.append({
            'speaker_id': speaker_id,
            'start': float(start_time),
            'end': float(end_time),
            'text': text.strip()
        })
        
    if not segments:
         print("⚠️ No segments parsed with primary regex, trying fallback.")
         pattern_single = re.compile(r'\[(SPEAKER_\w+)\] \[([\d.]+) - ([\d.]+)\]\s*(.*)')
         matches_single = pattern_single.findall(transcript_text)
         for match in matches_single:
            speaker_id, start_time, end_time, text = match
            segments.append({
                'speaker_id': speaker_id,
                'start': float(start_time),
                'end': float(end_time),
                'text': text.strip()
            })

    print(f"✅ Parsed {len(segments)} segments from STT transcript.")
    return segments

def merge_meeting_transcripts_by_time(job_id: str) -> bool:
    """
    Merges transcripts, adding formatted timestamps to the final output.
    """
    output_dir = os.path.join("outputs", job_id)
    captions_path = os.path.join(output_dir, "captions.jsonl")
    transcript_path = os.path.join(output_dir, "transcript.txt")
    merged_path = os.path.join(output_dir, "merged_transcript.txt")

    if not os.path.exists(captions_path):
        print(f"⚠️ Captions file not found: {captions_path}. Cannot merge.")
        return False
    with open(captions_path, 'r', encoding='utf-8') as f:
        captions = [json.loads(line) for line in f if line.strip()]
    if not captions:
        print("⚠️ Captions file is empty. Using STT transcript only.")
        return False
    print(f"✅ Loaded {len(captions)} captions.")

    if not os.path.exists(transcript_path):
        print(f"⚠️ STT transcript file not found: {transcript_path}. Cannot merge.")
        return False
    with open(transcript_path, 'r', encoding='utf-8') as f:
        transcript_content = f.read()
    stt_segments = parse_whisperx_transcript(transcript_content)
    if not stt_segments:
        print("❌ STT transcript could not be parsed. Aborting merge.")
        return False

    speaker_id_to_name_votes = defaultdict(lambda: defaultdict(int))
    for caption in captions:
        caption_time = caption.get('timestamp', -1)
        caption_speaker = caption.get('speaker', 'Unknown')
        if caption_time < 0: continue
        
        for segment in stt_segments:
            if segment['start'] <= caption_time <= segment['end']:
                speaker_id = segment['speaker_id']
                speaker_id_to_name_votes[speaker_id][caption_speaker] += 1
                break
    
    speaker_map = {}
    for speaker_id, votes in speaker_id_to_name_votes.items():
        if votes:
            winner_name = max(votes, key=votes.get)
            speaker_map[speaker_id] = winner_name
            print(f"✅ Mapped {speaker_id} -> {winner_name}")

    # --- FIXED: Write final transcript with formatted timestamps ---
    with open(merged_path, 'w', encoding='utf-8') as f:
        f.write("MERGED AND DIARIZED TRANSCRIPT\n")
        f.write("=" * 80 + "\n\n")
        
        for segment in stt_segments:
            speaker_name = speaker_map.get(segment['speaker_id'], segment['speaker_id'])
            # Get the formatted timestamp
            timestamp_str = format_seconds_to_hhmmss(segment['start'])
            
            f.write(f"[{timestamp_str}] {speaker_name}:\n")
            f.write(f"{segment['text']}\n\n")
            
    print(f"✅ Final merged transcript with timestamps saved to {merged_path}")
    return True

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python caption_merger.py <job_id>")
        sys.exit(1)
    
    job_id_arg = sys.argv[1]
    success = merge_meeting_transcripts_by_time(job_id_arg)
    sys.exit(0 if success else 1)
