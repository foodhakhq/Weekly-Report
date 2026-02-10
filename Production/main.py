# ---------------- Mood ----------------
if mood_data.get("status") == "ok":
    mood_records = mood_data.get("data") or []
    
    # Extract mood values with timestamps for tie-breaking when 3+ moods
    mood_entries = []
    for m in mood_records:
        if m.get("actual_value"):
            ts = m.get("timestamp") or m.get("date")
            mood_entries.append({
                "mood": m.get("actual_value"),
                "timestamp": ts
            })
    
    if not mood_entries:
        logger.debug("ℹ️ No mood data logged")
    else:
        # Count occurrences
        vals = [entry["mood"] for entry in mood_entries]
        unique_moods = len(set(vals))
        
        # Show mood mix if there's at least 1 mood entry
        if unique_moods >= 1:
            counts = Counter(vals).most_common(3)
            
            # Handle ties: random for 2 moods, chronological for 3+ moods
            if len(counts) >= 2:
                # Group by count
                count_groups = defaultdict(list)
                for mood_name, count in counts:
                    count_groups[count].append(mood_name)
                
                # Sort each group
                sorted_counts = []
                for count in sorted(count_groups.keys(), reverse=True):
                    moods_with_this_count = count_groups[count]
                    
                    # Only sort if there's a tie (2+ moods with same count)
                    if len(moods_with_this_count) >= 2:
                        if len(counts) == 2 and len(moods_with_this_count) == 2:
                            # CASE 1: Exactly 2 moods tied → Random
                            random.shuffle(moods_with_this_count)
                            logger.info(f"🎲 Mood Mix: 2-way tie, using random selection")
                        else:
                            # CASE 2: 3+ moods (or partial tie) → Chronological with fallback
                            # Try to parse timestamps for chronological ordering
                            first_occurrence = {}
                            timestamp_parse_failed = False
                            
                            for entry in mood_entries:
                                mood_name = entry["mood"]
                                if mood_name in moods_with_this_count and mood_name not in first_occurrence:
                                    try:
                                        ts = entry["timestamp"]
                                        dt = parse_dt_safe(ts)
                                        if dt:
                                            first_occurrence[mood_name] = dt
                                        else:
                                            timestamp_parse_failed = True
                                            break
                                    except:
                                        timestamp_parse_failed = True
                                        break
                            
                            # If all timestamps parsed successfully, use chronological
                            if not timestamp_parse_failed and len(first_occurrence) == len(moods_with_this_count):
                                moods_with_this_count.sort(key=lambda m: first_occurrence.get(m, datetime.max))
                                logger.info(f"⏰ Mood Mix: {len(moods_with_this_count)}-way tie, using chronological order")
                            else:
                                # Fallback: Random if timestamp parsing failed
                                random.shuffle(moods_with_this_count)
                                logger.info(f"🎲 Mood Mix: {len(moods_with_this_count)}-way tie, timestamp error - using random selection")
                    
                    for mood in moods_with_this_count:
                        sorted_counts.append((mood, count))
                
                counts = sorted_counts[:3]  # Keep top 3
            
            clusters = []
            sizes = ["Large", "Medium", "Small"]
            emojis = {
                "HAPPY": "😁",
                "SAD": "😔",
                "ENERGETIC": "😎",
                "FRISKY": "🤪",
                "MOOD SWINGS": "🫠",
                "IRRITATED": "🙄",
                "ANXIOUS": "😰",
                "DEPRESSED": "😞",
                "LOW ENERGY": "🤕",
                "CONFUSED": "😵‍💫",
                "APATHETIC": "😐",
                "CUSTOM": "😶"
            }
            
            for i, (mood_name, count) in enumerate(counts):
                clusters.append({
                    "mood_name": mood_name,
                    "emoji": emojis.get(mood_name, "😐"),
                    "count": count,
                    "visual_size": sizes[i] if i < 3 else "Small"
                })
            
            wellness["mood_mix"] = {"status": "active", "clusters": clusters}
            logger.info(f"😊 Mood Mix created: {len(clusters)} clusters (unique_moods={unique_moods})")
        else:
            logger.debug(f"ℹ️ No mood data logged")
else:
    logger.debug("ℹ️ No mood data found")
