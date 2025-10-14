"""
caption_merger.py - Utility to merge Google Meet captions with WhisperX transcripts

This merges real-time captions (with speaker names) with accurate STT transcripts
to produce a final transcript with both accuracy and speaker attribution.
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Tuple
from difflib import SequenceMatcher

class CaptionTranscriptMerger:
    def __init__(self, captions_path: str, transcript_path: str):
        self.captions_path = captions_path
        self.transcript_path = transcript_path
        self.captions = []
        self.transcript = ""
        
    def load_data(self):
        """Load captions and transcript from files."""
        # Load captions (JSONL format)
        if os.path.exists(self.captions_path):
            with open(self.captions_path, 'r', encoding='utf-8') as f:
                self.captions = [json.loads(line) for line in f if line.strip()]
            print(f"✅ Loaded {len(self.captions)} captions")
        else:
            print(f"⚠️ Captions file not found: {self.captions_path}")
            
        # Load transcript (plain text)
        if os.path.exists(self.transcript_path):
            with open(self.transcript_path, 'r', encoding='utf-8') as f:
                self.transcript = f.read().strip()
            print(f"✅ Loaded transcript ({len(self.transcript)} chars)")
        else:
            print(f"⚠️ Transcript file not found: {self.transcript_path}")
    
    def clean_text(self, text: str) -> str:
        """Normalize text for comparison."""
        import re
        text = text.lower()
        text = re.sub(r'[^\w\s]', '', text)  # Remove punctuation
        text = re.sub(r'\s+', ' ', text)      # Normalize whitespace
        return text.strip()
    
    def find_best_match(self, caption_text: str, transcript_words: List[str], 
                        start_idx: int, window_size: int = 50) -> Tuple[int, float]:
        """
        Find the best matching position in transcript for a caption.
        Returns (position, similarity_score)
        """
        caption_clean = self.clean_text(caption_text)
        caption_words = caption_clean.split()
        
        if not caption_words:
            return start_idx, 0.0
        
        best_pos = start_idx
        best_score = 0.0
        
        # Search within a window
        end_idx = min(start_idx + window_size, len(transcript_words) - len(caption_words))
        
        for i in range(start_idx, end_idx):
            # Get a slice of transcript words matching caption length
            transcript_slice = ' '.join(transcript_words[i:i+len(caption_words)])
            
            # Calculate similarity
            similarity = SequenceMatcher(None, caption_clean, transcript_slice).ratio()
            
            if similarity > best_score:
                best_score = similarity
                best_pos = i
        
        return best_pos, best_score
    
    def merge(self, output_path: str, similarity_threshold: float = 0.6):
        """
        Merge captions and transcript, using captions for speaker names
        and transcript for accuracy.
        """
        if not self.captions or not self.transcript:
            print("❌ Missing captions or transcript data")
            return False
        
        # Split transcript into words for matching
        transcript_words = self.clean_text(self.transcript).split()
        
        merged_segments = []
        current_pos = 0
        
        for i, caption in enumerate(self.captions):
            speaker = caption.get('speaker', 'Unknown')
            caption_text = caption.get('text', '').strip()
            timestamp = caption.get('timestamp', '')
            
            if not caption_text:
                continue
            
            # Find where this caption appears in the transcript
            match_pos, similarity = self.find_best_match(
                caption_text, 
                transcript_words, 
                current_pos
            )
            
            if similarity >= similarity_threshold:
                # Extract the corresponding text from the original transcript
                # This preserves punctuation and capitalization
                word_count = len(self.clean_text(caption_text).split())
                
                # Find actual position in original transcript
                clean_transcript = self.clean_text(self.transcript)
                clean_words = clean_transcript.split()
                
                # Get the matched segment from original transcript
                matched_words = []
                word_idx = 0
                char_idx = 0
                target_words = word_count
                
                # Walk through original transcript to extract exact text
                for char in self.transcript:
                    if word_idx < match_pos:
                        if char.isspace() and char_idx > 0 and self.transcript[char_idx-1].isspace() == False:
                            word_idx += 1
                    elif word_idx < match_pos + target_words:
                        matched_words.append(char)
                        if char.isspace() and char_idx > 0 and self.transcript[char_idx-1].isspace() == False:
                            word_idx += 1
                    else:
                        break
                    char_idx += 1
                
                matched_text = ''.join(matched_words).strip()
                
                merged_segments.append({
                    'speaker': speaker,
                    'text': matched_text if matched_text else caption_text,
                    'timestamp': timestamp,
                    'confidence': similarity
                })
                
                current_pos = match_pos + word_count
            else:
                # Low similarity - use caption as-is but flag it
                merged_segments.append({
                    'speaker': speaker,
                    'text': caption_text,
                    'timestamp': timestamp,
                    'confidence': similarity
                })
        
        # Write merged output
        with open(output_path, 'w', encoding='utf-8') as f:
            # Write header
            f.write("=" * 80 + "\n")
            f.write("MERGED TRANSCRIPT (Captions + STT)\n")
            f.write("=" * 80 + "\n\n")
            
            current_speaker = None
            for segment in merged_segments:
                speaker = segment['speaker']
                text = segment['text']
                timestamp = segment['timestamp']
                confidence = segment['confidence']
                
                # Add speaker label when speaker changes
                if speaker != current_speaker:
                    f.write(f"\n[{speaker}]:\n")
                    current_speaker = speaker
                
                # Write text with confidence indicator
                confidence_indicator = "✓" if confidence >= 0.8 else "~" if confidence >= 0.6 else "?"
                f.write(f"{text} {confidence_indicator}\n")
        
        print(f"✅ Merged transcript saved to {output_path}")
        print(f"   Total segments: {len(merged_segments)}")
        return True

def merge_meeting_transcripts(job_id: str):
    """Helper function to merge transcripts for a completed meeting."""
    output_dir = os.path.join("outputs", job_id)
    captions_path = os.path.join(output_dir, "captions.jsonl")
    transcript_path = os.path.join(output_dir, "transcript.txt")
    merged_path = os.path.join(output_dir, "merged_transcript.txt")
    
    merger = CaptionTranscriptMerger(captions_path, transcript_path)
    merger.load_data()
    return merger.merge(merged_path)

# Example usage
if __name__ == "__main__":
    import sys
    
    if len(sys.argv) != 2:
        print("Usage: python caption_merger.py <job_id>")
        sys.exit(1)
    
    job_id = sys.argv[1]
    success = merge_meeting_transcripts(job_id)
    sys.exit(0 if success else 1)
