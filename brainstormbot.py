import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
import json
import os
from datetime import datetime, timedelta
import pytz
import schedule
import threading
import time
import re
from io import BytesIO
import dotenv

# Load configuration from conf.env
CONFIG_FILE = "conf.env"

def load_config():
    """Load configuration from conf.env file or create template"""
    if not os.path.exists(CONFIG_FILE) or os.path.getsize(CONFIG_FILE) == 0:
        # Create template config file
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write("# Configuration file\n")
            f.write("TOKEN=\n")
            f.write("TEACHER_ID=\n")
            f.write("BREAK_TIME=20\n")
            f.write("LESSON_DURATION=60\n")
        print(f"⚠️  Created template config file: {CONFIG_FILE}")
        print("⚠️  Please fill in the configuration and restart the bot")
        return None

    # Load configuration
    config = {}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()

    # Validate required parameters - allow empty TEACHER_ID for initial setup
    required = ['TOKEN']
    missing = [param for param in required if param not in config or not config.get(param)]

    if missing:
        print(f"❌ Missing required parameters in {CONFIG_FILE}: {', '.join(missing)}")
        return None

    # Check if TEACHER_ID is set (can be blank initially)
    if 'TEACHER_ID' not in config:
        config['TEACHER_ID'] = ''

    return config

# Try to load configuration
config = load_config()
if config is None:
    exit(1)

# Configuration from file
TOKEN = config['TOKEN']
TEACHER_ID = config.get('TEACHER_ID', '')
BREAK_TIME = int(config.get('BREAK_TIME', '20'))  # Break time in minutes
LESSON_DURATION = int(config.get('LESSON_DURATION', '60'))  # Lesson duration in minutes

DATA_FILE = "lessons.json"
TIMEZONE_TEACHER = "Asia/Yekaterinburg"  # Екатеринбург (still hardcoded as requested)
TIMEZONE_STUDENT_DEFAULT = "Europe/Moscow"  # MSK as default

# Initialize bot
bot = telebot.TeleBot(TOKEN)

# Load or create data structure with ads support
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        "teacher_timetable": [],
        "lessons": [],
        "users": {},
        "settings": {"teacher_timezone": TIMEZONE_TEACHER},
        "advertisements": [],  # Store sent advertisements
        "last_timetable_week": None  # Track which week the timetable is for
    }

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# Initialize data
data = load_data()

# Helper functions
def parse_timetable_entry(text):
    """Parse timetable entry like 'ПН - 18:00-20:00' or 'DATE-WEEKDAY-TIME' format"""
    try:
        # Check if it's in DATE-WEEKDAY-TIME format (e.g., "15.12-ПН-18:00-20:00")
        date_pattern = r'(\d{1,2})\.(\d{1,2})-(\w{2,3})-(\d{1,2}:\d{2})-(\d{1,2}:\d{2})'
        date_match = re.match(date_pattern, text.strip())

        if date_match:
            day, month, weekday, start, end = date_match.groups()
            # Create a date (using current year)
            current_year = datetime.now().year
            try:
                date = datetime(current_year, int(month), int(day))
            except ValueError:
                # If date doesn't exist (e.g., 31.02), use current date as fallback
                date = datetime.now()

            return {
                "day": weekday.upper(),
                "date": date.strftime("%d.%m"),
                "start": start,
                "end": end,
                "original": text,
                "has_date": True,
                "full_date": date
            }

        # Original format: "ПН - 18:00-20:00"
        parts = text.split('-')
        if len(parts) >= 3:
            day_part = parts[0].strip().upper()
            time_part = '-'.join(parts[1:]).strip()

            # Extract time range
            time_range_match = re.search(r'(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})', time_part)
            if time_range_match:
                start, end = time_range_match.groups()
                return {
                    "day": day_part,
                    "start": start,
                    "end": end,
                    "original": text,
                    "has_date": False
                }

        # Default fallback
        return {
            "day": "UNKNOWN",
            "start": "18:00",
            "end": "20:00",
            "original": text,
            "has_date": False
        }
    except Exception as e:
        print(f"Error parsing timetable entry: {e}")
        return {
            "day": "UNKNOWN",
            "start": "18:00",
            "end": "20:00",
            "original": text,
            "has_date": False
        }

def create_time_slots(entry, date=None):
    """Create lesson slots from a timetable entry with breaks"""
    slots = []

    # Use provided date or generate from day
    if date is None:
        date = get_next_weekday(entry['day'], 0)
        if date is None:
            return []

    start_hour = int(entry['start'].split(':')[0])
    start_min = int(entry['start'].split(':')[1])
    end_hour = int(entry['end'].split(':')[0])
    end_min = int(entry['end'].split(':')[1])

    current_time = datetime(date.year, date.month, date.day, start_hour, start_min)
    end_time = datetime(date.year, date.month, date.day, end_hour, end_min)

    while current_time + timedelta(minutes=LESSON_DURATION) <= end_time:
        slot_end = current_time + timedelta(minutes=LESSON_DURATION)

        slots.append({
            "start": current_time.strftime("%H:%M"),
            "end": slot_end.strftime("%H:%M"),
            "datetime": current_time
        })

        # Add break time after each slot
        current_time = slot_end + timedelta(minutes=BREAK_TIME)

    return slots

def convert_time_for_student(teacher_time, student_timezone=TIMEZONE_STUDENT_DEFAULT):
    """Convert teacher's time to student's timezone"""
    teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
    student_tz = pytz.timezone(student_timezone)

    # Create datetime with teacher's timezone
    teacher_dt = teacher_tz.localize(teacher_time)
    # Convert to student's timezone
    student_dt = teacher_dt.astimezone(student_tz)

    return student_dt, student_tz.zone

def get_next_weekday(day_abbr, weeks_ahead=0):
    """Get next date for given weekday abbreviation (ПН, ВТ, etc.)"""
    days_map = {
        "ПН": 0, "MON": 0,
        "ВТ": 1, "TUE": 1,
        "СР": 2, "WED": 2,
        "ЧТ": 3, "THU": 3,
        "ПТ": 4, "FRI": 4,
        "СБ": 5, "SAT": 5,
        "ВС": 6, "SUN": 6
    }

    if day_abbr not in days_map:
        return None

    target_day = days_map[day_abbr]
    today = datetime.now(pytz.timezone(TIMEZONE_TEACHER))
    days_ahead = target_day - today.weekday()
    if days_ahead <= 0:
        days_ahead += 7

    # Add weeks
    days_ahead += (7 * weeks_ahead)
    return today + timedelta(days=days_ahead)

def is_slot_available(proposed_datetime):
    """Check if a time slot is available (not overlapping with existing bookings)"""
    proposed_end = proposed_datetime + timedelta(minutes=LESSON_DURATION)

    for lesson in data['lessons']:
        if lesson.get('status') == 'cancelled' or lesson.get('status') == 'cancelled_by_teacher':
            continue

        lesson_start = datetime.fromisoformat(lesson['datetime'])
        lesson_end = lesson_start + timedelta(minutes=LESSON_DURATION)

        # Check for overlap
        if not (proposed_end <= lesson_start or proposed_datetime >= lesson_end):
            return False

    return True

def get_current_week_number():
    """Get current week number (ISO week number)"""
    return datetime.now(pytz.timezone(TIMEZONE_TEACHER)).isocalendar()[1]

def is_timetable_for_current_week():
    """Check if timetable is set for current week"""
    current_week = get_current_week_number()
    return data.get('last_timetable_week') == current_week

def get_available_slots_for_user(user_id):
    """Get all available slots for current week only"""
    available_slots = []

    # Check if timetable is set for current week
    if not is_timetable_for_current_week():
        return available_slots

    user_tz = data['users'][user_id]['timezone']

    # Only generate slots for current week (week_offset = 0)
    for entry in data['teacher_timetable']:
        if entry.get('has_date'):
            # Entry has specific date
            date = entry.get('full_date')
            if date is None:
                continue
            # Check if date is in current week
            entry_week = date.isocalendar()[1]
            current_week = get_current_week_number()
            if entry_week != current_week:
                continue
        else:
            # Entry is weekly - only for current week
            date = get_next_weekday(entry['day'], 0)
            if date is None:
                continue

        slots = create_time_slots(entry, date)

        # Convert time for student and check availability
        for slot in slots:
            if is_slot_available(slot['datetime']):
                student_dt, tz_name = convert_time_for_student(slot['datetime'], user_tz)

                # Format date with weekday
                date_str = date.strftime("%d.%m")
                weekday_map = {
                    0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ",
                    4: "ПТ", 5: "СБ", 6: "ВС"
                }
                weekday = weekday_map[date.weekday()]

                slot_info = {
                    "date_obj": date,
                    "date_str": f"{date_str}-{weekday}",
                    "teacher_time": f"{slot['start']}-{slot['end']}",
                    "student_time": student_dt.strftime("%H:%M"),
                    "datetime": slot['datetime'],
                    "day": weekday,
                    "timezone": tz_name
                }
                available_slots.append(slot_info)

    # Sort by date and time
    available_slots.sort(key=lambda x: x['datetime'])
    return available_slots

# Admin panel functions
def is_teacher(user_id):
    """Check if user is the teacher - only works if TEACHER_ID is set"""
    return TEACHER_ID and str(user_id) == TEACHER_ID

def admin_panel(message):
    if not is_teacher(message.from_user.id):
        return

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        KeyboardButton("📊 Статистика"),
        KeyboardButton("👥 Список студентов"),
        KeyboardButton("💰 Пополнить уроки"),
        KeyboardButton("📅 Текущее расписание"),
        KeyboardButton("❌ Удалить запись"),
        KeyboardButton("📢 Отправить рекламу")
    )

    bot.send_message(
        message.chat.id,
        "👨‍🏫 **Админ-панель**\n\nВыберите действие:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

# Bot handlers
@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = str(message.from_user.id)
    user_name = message.from_user.first_name
    if message.from_user.last_name:
        user_name += " " + message.from_user.last_name

    # Initialize user if not exists
    if user_id not in data['users']:
        data['users'][user_id] = {
            "name": user_name,
            "remaining": 0,
            "phone": None,
            "schedule": [],
            "timezone": TIMEZONE_STUDENT_DEFAULT
        }
        save_data(data)

    # Check if teacher has set up their ID
    if not TEACHER_ID:
        # Show setup instructions for everyone
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("📚 Записаться на урок"))
        markup.add(KeyboardButton("📅 Мои записи"))
        markup.add(KeyboardButton("❌ Отменить запись"))
        markup.add(KeyboardButton("ℹ️ Осталось уроков"))
        markup.add(KeyboardButton("🕐 Установить часовой пояс"))
        markup.add(KeyboardButton("📞 Указать телефон"))

        bot.send_message(
            message.chat.id,
            f"👋 **Привет, {user_name}!**\n\nЯ бот для записи на уроки. Вы можете:\n"
            "• Записаться на урок\n• Посмотреть/отменить записи\n• Проверить остаток уроков\n• Установить часовой пояс\n• Если застряли в боте, просто пропишите /start снова",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    elif is_teacher(message.from_user.id):
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("📝 Установить расписание"))
        markup.add(KeyboardButton("👨‍🏫 Админ-панель"))
        bot.send_message(
            message.chat.id,
            f"👨‍🏫 **Привет, учитель!**\n\n"
            f"Текущее время перерыва: {BREAK_TIME} минут\n"
            f"Длительность урока: {LESSON_DURATION} минут\n"
            f"Вы можете установить свое расписание или перейти в админ-панель.",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("📚 Записаться на урок"))
        markup.add(KeyboardButton("📅 Мои записи"))
        markup.add(KeyboardButton("❌ Отменить запись"))
        markup.add(KeyboardButton("ℹ️ Осталось уроков"))
        markup.add(KeyboardButton("🕐 Установить часовой пояс"))
        markup.add(KeyboardButton("📞 Указать телефон"))

        bot.send_message(
            message.chat.id,
            f"👋 **Привет, {user_name}!**\n\nЯ бот для записи на уроки. Вы можете:\n"
            "• Записаться на урок\n• Посмотреть/отменить записи\n• Проверить остаток уроков\n• Установить часовой пояс\n• Если застряли в боте, просто пропишите /start снова",
            reply_markup=markup,
            parse_mode="Markdown"
        )


@bot.message_handler(func=lambda message: message.text == "❌ Отменить запись" and not is_teacher(message.from_user.id))
def cancel_booking_student(message):
    """Show student's bookings for cancellation"""
    user_id = str(message.from_user.id)

    # Reload data to prevent infinite cancellation bug
    global data
    data = load_data()

    # Get user's upcoming lessons
    upcoming_lessons = []
    now = datetime.now()

    for lesson in data['lessons']:
        if lesson['student_id'] == user_id and lesson.get('status') not in ['cancelled', 'cancelled_by_teacher']:
            lesson_dt = datetime.fromisoformat(lesson['datetime'])
            if lesson_dt > now:
                upcoming_lessons.append(lesson)

    if not upcoming_lessons:
        bot.send_message(message.chat.id, "📭 У вас нет запланированных уроков.")
        return

    markup = InlineKeyboardMarkup()
    for i, lesson in enumerate(upcoming_lessons[:10]):  # Limit to 10
        dt = datetime.fromisoformat(lesson['datetime'])
        date_str = dt.strftime("%d.%m-%a-%H:%M").replace("Mon", "ПН").replace("Tue", "ВТ").replace("Wed", "СР")\
                   .replace("Thu", "ЧТ").replace("Fri", "ПТ").replace("Sat", "СБ").replace("Sun", "ВС")
        # Add two buttons per lesson: cancel and reschedule
        markup.add(
            InlineKeyboardButton(
                f"❌ {i+1}. {date_str}",
                callback_data=f"cancel_student_{lesson['id']}"
            ),
            InlineKeyboardButton(
                f"🔄 Перенести {i+1}",
                callback_data=f"reschedule_student_{lesson['id']}"
            )
        )

    bot.send_message(
        message.chat.id,
        "❌ **Управление записями:**\n\n"
        "*Выберите действие для урока:*\n"
        "❌ - Отменить урок (возврат в баланс)\n"
        "🔄 - Перенести на другое время\n\n"
        "*Отмена возможна минимум за 2 часа до урока*",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.message_handler(func=lambda message: message.text == "❌ Удалить запись" and is_teacher(message.from_user.id))
def delete_booking_teacher(message):
    """Show all bookings for teacher to delete"""
    # Reload data to prevent infinite cancellation bug
    global data
    data = load_data()

    # Get upcoming lessons
    upcoming_lessons = []
    now = datetime.now()

    for lesson in data['lessons']:
        if lesson.get('status') not in ['cancelled', 'cancelled_by_teacher']:
            lesson_dt = datetime.fromisoformat(lesson['datetime'])
            if lesson_dt > now:
                upcoming_lessons.append(lesson)

    if not upcoming_lessons:
        bot.send_message(message.chat.id, "📭 Нет запланированных уроков.")
        return

    markup = InlineKeyboardMarkup()
    for i, lesson in enumerate(upcoming_lessons[:15]):  # Limit to 15
        dt = datetime.fromisoformat(lesson['datetime'])
        date_str = dt.strftime("%d.%m %H:%M")
        markup.add(InlineKeyboardButton(
            f"{i+1}. {date_str} - {lesson['student_name']}",
            callback_data=f"delete_teacher_{lesson['id']}"
        ))

    bot.send_message(
        message.chat.id,
        "❌ **Выберите запись для удаления:**",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_student_'))
def process_cancel_student(call):
    """Process student cancellation"""
    user_id = str(call.from_user.id)
    lesson_id = call.data.replace('cancel_student_', '')

    # Reload data to ensure we have the latest state and prevent infinite cancellation bug
    global data
    data = load_data()

    # Find the lesson
    lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)

    if not lesson or lesson['student_id'] != user_id:
        bot.answer_callback_query(call.id, "❌ Запись не найдена")
        return

    # Check if lesson is already cancelled
    if lesson.get('status') in ['cancelled', 'cancelled_by_teacher']:
        bot.answer_callback_query(call.id, "❌ Этот урок уже отменен")
        return

    # Check if cancellation is allowed (at least 2 hours before)
    lesson_dt = datetime.fromisoformat(lesson['datetime'])
    time_diff = lesson_dt - datetime.now()

    if time_diff.total_seconds() < 7200:  # 2 hours in seconds
        bot.answer_callback_query(call.id, "❌ Отмена возможна только за 2+ часа до урока")
        return

    # Refund lesson to student
    data['users'][user_id]['remaining'] += 1
    if lesson_id in data['users'][user_id]['schedule']:
        data['users'][user_id]['schedule'].remove(lesson_id)
    lesson['status'] = 'cancelled'

    # Save and reload data
    save_data(data)
    data = load_data()

    # Notify student
    dt = datetime.fromisoformat(lesson['datetime'])
    date_str = dt.strftime("%d.%m.%Y %H:%M")

    bot.edit_message_text(
        f"✅ **Запись отменена!**\n\n"
        f"📅 Дата: {date_str}\n"
        f"🔄 Урок возвращен в баланс\n"
        f"📊 Осталось уроков: {data['users'][user_id]['remaining']}",
        call.message.chat.id,
        call.message.message_id
    )

    # Notify teacher
    if TEACHER_ID:
        bot.send_message(
            TEACHER_ID,
            f"❌ **Отмена урока студентом**\n\n"
            f"👨‍🎓 Студент: {lesson['student_name']}\n"
            f"📅 Дата: {date_str}\n"
            f"🔄 Урок возвращен студенту",
            parse_mode="Markdown"
        )

    bot.answer_callback_query(call.id, "✅ Запись отменена")

@bot.callback_query_handler(func=lambda call: call.data.startswith('reschedule_student_'))
def process_reschedule_student(call):
    """Process student rescheduling a lesson"""
    user_id = str(call.from_user.id)
    lesson_id = call.data.replace('reschedule_student_', '')

    # Reload data to ensure we have the latest state
    global data
    data = load_data()

    # Find the lesson
    lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)

    if not lesson or lesson['student_id'] != user_id:
        bot.answer_callback_query(call.id, "❌ Запись не найдена")
        return

    # Check if lesson is already cancelled
    if lesson.get('status') in ['cancelled', 'cancelled_by_teacher']:
        bot.answer_callback_query(call.id, "❌ Этот урок уже отменен")
        return

    # Check if reschedule is allowed (at least 2 hours before)
    lesson_dt = datetime.fromisoformat(lesson['datetime'])
    time_diff = lesson_dt - datetime.now()

    if time_diff.total_seconds() < 7200:  # 2 hours in seconds
        bot.answer_callback_query(call.id, "❌ Перенос возможен только за 2+ часа до урока")
        return

    # Check if timetable is set for current week
    if not is_timetable_for_current_week():
        bot.answer_callback_query(call.id, "❌ Расписание на эту неделю еще не установлено")
        return

    # Get available slots for rescheduling
    available_slots = get_available_slots_for_user(user_id)

    # Filter out the current lesson time and conflicting times
    filtered_slots = []
    for slot in available_slots:
        # Skip the current lesson time
        if slot['datetime'] == lesson_dt:
            continue
        # Check if slot doesn't conflict with other lessons
        if is_slot_available(slot['datetime']):
            # Check user's existing lessons for conflicts
            conflict = False
            for other_lesson_id in data['users'][user_id]['schedule']:
                if other_lesson_id == lesson_id:
                    continue
                other_lesson = next((l for l in data['lessons'] if l['id'] == other_lesson_id), None)
                if other_lesson and other_lesson.get('status') not in ['cancelled', 'cancelled_by_teacher']:
                    other_start = datetime.fromisoformat(other_lesson['datetime'])
                    other_end = other_start + timedelta(minutes=LESSON_DURATION)
                    proposed_end = slot['datetime'] + timedelta(minutes=LESSON_DURATION)
                    if not (proposed_end <= other_start or slot['datetime'] >= other_end):
                        conflict = True
                        break
            if not conflict:
                filtered_slots.append(slot)

    if not filtered_slots:
        bot.answer_callback_query(call.id, "❌ Нет доступных слотов для переноса")
        return

    # Store lesson ID in user data for rescheduling
    if user_id not in data['users']:
        data['users'][user_id] = {}
    data['users'][user_id]['rescheduling_lesson_id'] = lesson_id
    save_data(data)

    # Create keyboard with available slots
    markup = InlineKeyboardMarkup()
    for i, slot in enumerate(filtered_slots[:25]):
        button_text = f"{slot['date_str']} - {slot['student_time']} ({slot['timezone']})"
        callback_data = f"reschedule_confirm_{i}_{slot['day']}_{slot['datetime'].strftime('%Y%m%d_%H%M')}_{lesson_id}"
        markup.add(InlineKeyboardButton(button_text, callback_data=callback_data))

    bot.edit_message_text(
        f"🔄 **Перенос урока**\\n\\n"
        f"Текущая запись: {lesson_dt.strftime('%d.%m-%a-%H:%M').replace('Mon', 'ПН').replace('Tue', 'ВТ').replace('Wed', 'СР').replace('Thu', 'ЧТ').replace('Fri', 'ПТ').replace('Sat', 'СБ').replace('Sun', 'ВС')}\\n\\n"
        f"Выберите новое время для урока:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="Markdown"
    )
    bot.answer_callback_query(call.id, "Выберите новое время")

@bot.callback_query_handler(func=lambda call: call.data.startswith('reschedule_confirm_'))
def confirm_reschedule(call):
    """Confirm the reschedule action"""
    user_id = str(call.from_user.id)
    parts = call.data.split('_')
    
    if len(parts) < 6:
        bot.answer_callback_query(call.id, "❌ Ошибка в данных")
        return

    lesson_id = parts[5]
    datetime_str = f"{parts[4]}_{parts[5]}" if len(parts) > 5 else f"{parts[3]}_{parts[4]}"
    
    # Re-parse to get correct indices
    # Format: reschedule_confirm_IDX_DAY_YYYYMMDD_HHMM_LESSONID
    if len(parts) == 6:
        # reschedule_confirm_IDX_DAY_YYYYMMDD_HHMM (lesson_id at end was wrong)
        # Actually: reschedule_confirm_IDX_DAY_YYYYMMDD_HHMM_LESSONID
        datetime_str = f"{parts[3]}_{parts[4]}"
        lesson_id = parts[5] if len(parts) > 5 else None
    
    # Better parsing
    data_parts = call.data.replace('reschedule_confirm_', '').split('_')
    if len(data_parts) < 4:
        bot.answer_callback_query(call.id, "❌ Ошибка в данных")
        return
    
    day = data_parts[1]
    datetime_str = f"{data_parts[2]}_{data_parts[3]}"
    original_lesson_id = data_parts[4] if len(data_parts) > 4 else None

    try:
        proposed_datetime = datetime.strptime(datetime_str, "%Y%m%d_%H%M")
        
        # Verify the lesson still exists
        lesson = next((l for l in data['lessons'] if l['id'] == original_lesson_id), None)
        if not lesson or lesson['student_id'] != user_id:
            bot.answer_callback_query(call.id, "❌ Запись не найдена")
            return
        
        # Check if slot is still available
        if not is_slot_available(proposed_datetime):
            bot.answer_callback_query(call.id, "❌ Этот слот уже занят")
            return
        
        # Update the lesson datetime
        old_datetime = lesson['datetime']
        lesson['datetime'] = proposed_datetime.isoformat()
        
        # Save data
        save_data(data)
        
        # Format dates
        teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
        localized_new = teacher_tz.localize(proposed_datetime)
        weekday_map = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}
        weekday = weekday_map[localized_new.weekday()]
        new_date_str = localized_new.strftime(f"%d.%m-{weekday}-%H:%M")
        end_time = localized_new + timedelta(minutes=LESSON_DURATION)
        
        bot.edit_message_text(
            f"✅ **Урок перенесен!**\\n\\n"
            f"📅 Новая дата: {new_date_str}\\n"
            f"⏰ Время: {localized_new.strftime('%H:%M')}-{end_time.strftime('%H:%M')} (время учителя)\\n"
            f"👨‍🎓 Студент: {lesson['student_name']}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )
        
        # Notify teacher
        if TEACHER_ID:
            bot.send_message(
                TEACHER_ID,
                f"🔄 **Студент перенес урок**\\n\\n"
                f"👨‍🎓 Студент: {lesson['student_name']}\\n"
                f"📅 Новая дата: {new_date_str}\\n"
                f"⏰ Время: {localized_new.strftime('%H:%M')}-{end_time.strftime('%H:%M')}",
                parse_mode="Markdown"
            )
        
        # Schedule new reminder
        schedule_reminder(lesson['id'], proposed_datetime)
        
        bot.answer_callback_query(call.id, "✅ Урок успешно перенесен!")
        
    except Exception as e:
        print(f"Error in reschedule: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при переносе")

@bot.callback_query_handler(func=lambda call: call.data.startswith('delete_teacher_'))
def process_delete_teacher(call):
    """Process teacher deletion of booking"""
    lesson_id = call.data.replace('delete_teacher_', '')

    # Reload data to ensure we have the latest state and prevent infinite cancellation bug
    global data
    data = load_data()

    # Find the lesson
    lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)

    if not lesson:
        bot.answer_callback_query(call.id, "❌ Запись не найдена")
        return

    # Check if lesson is already cancelled
    if lesson.get('status') in ['cancelled', 'cancelled_by_teacher']:
        bot.answer_callback_query(call.id, "❌ Этот урок уже отменен")
        return

    # Get student info
    student_id = lesson['student_id']

    # Refund lesson to student
    if student_id in data['users']:
        data['users'][student_id]['remaining'] += 1
        if lesson_id in data['users'][student_id]['schedule']:
            data['users'][student_id]['schedule'].remove(lesson_id)

    # Remove lesson
    lesson['status'] = 'cancelled_by_teacher'

    # Save and reload data
    save_data(data)
    data = load_data()

    # Notify teacher
    dt = datetime.fromisoformat(lesson['datetime'])
    date_str = dt.strftime("%d.%m.%Y %H:%M")

    bot.edit_message_text(
        f"✅ **Запись удалена!**\n\n"
        f"👨‍🎓 Студент: {lesson['student_name']}\n"
        f"📅 Дата: {date_str}\n"
        f"🔄 Урок возвращен студенту",
        call.message.chat.id,
        call.message.message_id
    )

    # Notify student if possible
    try:
        bot.send_message(
            student_id,
            f"❌ **Урок отменен учителем**\n\n"
            f"📅 Дата: {date_str}\n"
            f"🔄 Урок возвращен в ваш баланс\n"
            f"📊 Осталось уроков: {data['users'][student_id]['remaining']}",
            parse_mode="Markdown"
        )
    except:
        pass  # Student may have blocked the bot

    bot.answer_callback_query(call.id, "✅ Запись удалена")

# UPDATED ADVERTISEMENT SYSTEM
@bot.message_handler(func=lambda message: message.text == "📢 Отправить рекламу" and is_teacher(message.from_user.id))
def request_advertisement(message):
    """Request advertisement content from teacher"""
    msg = bot.send_message(
        message.chat.id,
        "📢 **Создание рекламного сообщения**\n\n"
        "Отправьте рекламное сообщение (текст, фото с подписью, или и то и другое).\n"
        "Сообщение будет отправлено всем студентам сразу после подтверждения.\n\n"
        "Или отправьте 'отмена' для отмены.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_advertisement_content)

def process_advertisement_content(message):
    """Process advertisement content and send immediately after confirmation"""
    if message.text and message.text.lower() == 'отмена':
        bot.send_message(message.chat.id, "❌ Создание рекламы отменено.")
        return

    # Prepare advertisement object
    advertisement = {
        "id": f"ad_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "created_at": datetime.now().isoformat(),
        "has_text": False,
        "has_photo": False,
        "text": "",
        "photo": None,
        "caption": ""
    }

    # Extract content based on message type
    if message.content_type == 'text':
        advertisement["has_text"] = True
        advertisement["text"] = message.text
    elif message.content_type == 'photo':
        advertisement["has_photo"] = True
        advertisement["photo"] = message.photo[-1].file_id
        if message.caption:
            advertisement["has_text"] = True
            advertisement["caption"] = message.caption
            advertisement["text"] = message.caption
    elif message.content_type == 'document' or message.content_type == 'video':
        bot.send_message(message.chat.id, "❌ Поддерживаются только текст и фото. Попробуйте снова.")
        return

    # Ask for confirmation
    preview_text = "📢 **Предпросмотр рекламы:**\n\n"

    if advertisement["has_text"]:
        preview_text += advertisement["text"] + "\n\n"

    # Count students (excluding teacher)
    student_count = len([uid for uid in data['users'] if uid != TEACHER_ID])
    preview_text += f"Получателей: {student_count} студентов\n"
    preview_text += "Отправить это сообщение всем студентам? (отправитель будет скрыт)"

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("✅ Отправить всем", callback_data=f"ad_send_{message.message_id}"),
        InlineKeyboardButton("❌ Отменить", callback_data="ad_cancel")
    )

    # Store advertisement temporarily with message content
    data.setdefault('temp_ads', {})[str(message.message_id)] = advertisement
    save_data(data)

    # Show preview
    if advertisement["has_photo"]:
        bot.send_photo(
            message.chat.id,
            advertisement["photo"],
            caption=preview_text,
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        bot.send_message(
            message.chat.id,
            preview_text,
            reply_markup=markup,
            parse_mode="Markdown"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('ad_'))
def handle_advertisement_actions(call):
    """Handle advertisement actions"""
    if call.data == "ad_cancel":
        bot.edit_message_text(
            "❌ Рекламная рассылка отменена",
            call.message.chat.id,
            call.message.message_id
        )
        bot.answer_callback_query(call.id)
        return

    # Handle send action
    if call.data.startswith('ad_send_'):
        msg_id = call.data.replace('ad_send_', '')

        # Get advertisement from temp storage
        if 'temp_ads' not in data or msg_id not in data['temp_ads']:
            bot.answer_callback_query(call.id, "❌ Сообщение не найдено")
            return

        advertisement = data['temp_ads'][msg_id]

        # Send to all students (forward with sender hidden)
        success_count = 0
        fail_count = 0

        for user_id, user_data in list(data['users'].items()):
            if user_id == TEACHER_ID:
                continue

            try:
                if advertisement["has_photo"]:
                    # Send photo with caption (forwarding not possible with captions, so send as new message)
                    bot.send_photo(
                        user_id,
                        advertisement["photo"],
                        caption=advertisement["text"] if advertisement["has_text"] else None,
                        parse_mode="Markdown"
                    )
                elif advertisement["has_text"]:
                    # Send text message
                    bot.send_message(
                        user_id,
                        advertisement["text"],
                        parse_mode="Markdown"
                    )
                success_count += 1
            except Exception as e:
                print(f"Failed to send ad to {user_id}: {e}")
                fail_count += 1

        # Store sent advertisement in permanent storage
        sent_ad = advertisement.copy()
        sent_ad.update({
            "sent": True,
            "sent_at": datetime.now().isoformat(),
            "success_count": success_count,
            "fail_count": fail_count
        })

        if 'advertisements' not in data:
            data['advertisements'] = []
        data['advertisements'].append(sent_ad)

        # Clean up temp storage
        if 'temp_ads' in data and msg_id in data['temp_ads']:
            del data['temp_ads'][msg_id]

        save_data(data)

        # Update confirmation message
        bot.edit_message_text(
            f"✅ **Реклама отправлена!**\n\n"
            f"✅ Успешно: {success_count}\n"
            f"❌ Не удалось: {fail_count}\n"
            f"📊 Всего получателей: {len(data['users']) - 1}",
            call.message.chat.id,
            call.message.message_id
        )

        bot.answer_callback_query(call.id, "✅ Реклама отправлена")

@bot.message_handler(func=lambda message: message.text == "📝 Установить расписание" and is_teacher(message.from_user.id))
def request_timetable(message):
    # Clear previous timetable for the week
    data['teacher_timetable'] = []

    msg = bot.send_message(
        message.chat.id,
        f"📅 **Установите расписание**\n\n"
        f"Текущее время перерыва: {BREAK_TIME} минут\n"
        f"Длительность урока: {LESSON_DURATION} минут\n\n"
        "Выберите формат:\n"
        "1. **Еженедельное**: 'День - Время'\n"
        "   Пример: 'ПН - 18:00-20:00'\n"
        "2. **На конкретную дату**: 'ДАТА-ДЕНЬ-ВРЕМЯ'\n"
        "   Пример: '15.12-ПН-18:00-20:00'\n\n"
        "ВНИМАНИЕ: Это расписание только на текущую неделю!\n"
        "В следующую неделю нужно будет создать новое расписание.\n\n"
        "Введите расписание (несколько строк подряд) и отправьте 'готово'.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_timetable)

def process_timetable(message):
    if message.text.lower() == 'готово':
        # Mark timetable as set for current week
        current_week = get_current_week_number()
        data['last_timetable_week'] = current_week
        save_data(data)

        bot.send_message(
            message.chat.id,
            f"✅ Расписание установлено на текущую неделю (неделя {current_week})!\n"
            f"Доступно {len(data['teacher_timetable'])} временных окон.\n"
            f"Длительность урока: {LESSON_DURATION} минут\n"
            f"Время перерыва между уроками: {BREAK_TIME} минут\n\n📅 В воскресенье утром студентам будет отправлено уведомление с расписанием на неделю.",
            reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(
                KeyboardButton("📝 Установить расписание"),
                KeyboardButton("👨‍🏫 Админ-панель")
            )
        )
        return

    entry = parse_timetable_entry(message.text)
    if entry:
        data['teacher_timetable'].append(entry)
        save_data(data)

        if entry.get('has_date'):
            date_str = entry.get('date', 'N/A')
            bot.send_message(message.chat.id, f"✅ Добавлено на {date_str}: {entry['start']}-{entry['end']} (урок: {LESSON_DURATION} мин, перерыв: {BREAK_TIME} мин)")
        else:
            bot.send_message(message.chat.id, f"✅ Добавлено: {entry['day']} {entry['start']}-{entry['end']} (урок: {LESSON_DURATION} мин, перерыв: {BREAK_TIME} мин)")
    else:
        bot.send_message(message.chat.id, "❌ Неверный формат. Попробуйте снова.")

    bot.register_next_step_handler(message, process_timetable)

@bot.message_handler(func=lambda message: message.text == "📚 Записаться на урок" and not is_teacher(message.from_user.id))
def show_available_slots(message):
    user_id = str(message.from_user.id)

    if data['users'][user_id]['remaining'] <= 0:
        bot.send_message(
            message.chat.id,
            "❌ У вас не осталось уроков для записи. Обратитесь к @CanEUHearMe (ваш учитель) для пополнения.",
            reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(
                KeyboardButton("📚 Записаться на урок"),
                KeyboardButton("📅 Мои записи"),
                KeyboardButton("❌ Отменить запись")
            )
        )
        return

    # Check if timetable is set for current week
    if not is_timetable_for_current_week():
        bot.send_message(
            message.chat.id,
            "📭 На эту неделю расписание еще не установлено учителем.\n"
            "Пожалуйста, подождите, пока учитель обновит расписание.",
            parse_mode="Markdown"
        )
        return

    available_slots = get_available_slots_for_user(user_id)

    if not available_slots:
        bot.send_message(
            message.chat.id,
            "📭 На эту неделю нет доступных слотов для записи.\n"
            "Все слоты заняты или расписание не установлено.",
            parse_mode="Markdown"
        )
        return

    # Group slots by day
    slots_by_day = {}
    for slot in available_slots:
        day_key = slot['date_str']  # e.g., "22.06-ПН"
        if day_key not in slots_by_day:
            slots_by_day[day_key] = []
        slots_by_day[day_key].append(slot)

    # Create keyboard with slots grouped by day
    markup = InlineKeyboardMarkup()
    
    # Sort days chronologically
    sorted_days = sorted(slots_by_day.keys(), key=lambda x: datetime.strptime(x.split('-')[0], "%d.%m"))
    
    for day_key in sorted_days:
        day_slots = slots_by_day[day_key]
        
        # Add a non-clickable label for the day (using a disabled button trick)
        # Since Telegram doesn't support disabled buttons, we'll use a text message approach
        # Instead, we'll group all slots for this day together
        
        for slot in day_slots:
            # Format: "DATE-WEEKDAY-TIME (TIMEZONE)"
            button_text = f"{slot['student_time']} ({slot['timezone']})"
            callback_data = f"book_{slot['day']}_{slot['datetime'].strftime('%Y%m%d_%H%M')}_{slot['date_str']}"
            markup.add(InlineKeyboardButton(button_text, callback_data=callback_data))
        
        # Add a separator line (empty row) between days for visual clarity
        # We can't add empty rows, so we'll just continue to next day

    bot.send_message(
        message.chat.id,
        f"📅 **Доступные слоты для записи:**\n\n"
        f"*Время указано в вашем часовом поясе*\n"
        f"Осталось уроков: {data['users'][user_id]['remaining']}\n"
        f"Длительность урока: {LESSON_DURATION} минут\n\n"
        f"🗓 *Расписание по дням:*",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('book_'))
def book_slot(call):
    user_id = str(call.from_user.id)
    user_data = data['users'][user_id]

    if user_data['remaining'] <= 0:
        bot.answer_callback_query(call.id, "❌ У вас не осталось уроков")
        return

    parts = call.data.split('_')
    # New format: book_DAY_YYYYMMDD_HHMM_DATESTR (e.g., book_ПН_20250622_1800_22.06-ПН)
    if len(parts) < 5:
        bot.answer_callback_query(call.id, "❌ Ошибка в данных")
        return

    day = parts[1]
    datetime_str = f"{parts[2]}_{parts[3]}"
    date_str = parts[4]  # e.g., "22.06-ПН"

    try:
        proposed_datetime = datetime.strptime(datetime_str, "%Y%m%d_%H%M")

        if not is_slot_available(proposed_datetime):
            bot.answer_callback_query(call.id, "❌ Этот слот уже занят")
            return

        # Check user's existing lessons for conflicts
        for lesson_id in user_data['schedule']:
            lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)
            if lesson and lesson.get('status') not in ['cancelled', 'cancelled_by_teacher']:
                lesson_start = datetime.fromisoformat(lesson['datetime'])
                lesson_end = lesson_start + timedelta(minutes=LESSON_DURATION)
                proposed_end = proposed_datetime + timedelta(minutes=LESSON_DURATION)

                if not (proposed_end <= lesson_start or proposed_datetime >= lesson_end):
                    bot.answer_callback_query(
                        call.id,
                        "❌ У вас уже есть урок в это время"
                    )
                    return

        # Create lesson
        lesson_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{user_id}"
        lesson = {
            "id": lesson_id,
            "student_id": user_id,
            "student_name": user_data['name'],
            "datetime": proposed_datetime.isoformat(),
            "teacher_timezone": TIMEZONE_TEACHER,
            "status": "scheduled",
            "duration": LESSON_DURATION
        }

        data['lessons'].append(lesson)
        user_data['schedule'].append(lesson_id)
        user_data['remaining'] -= 1
        save_data(data)

        # Format date with weekday
        teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
        localized_dt = teacher_tz.localize(proposed_datetime)
        weekday_map = {
            0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ",
            4: "ПТ", 5: "СБ", 6: "ВС"
        }
        weekday = weekday_map[localized_dt.weekday()]
        date_str = localized_dt.strftime(f"%d.%m-{weekday}-%H:%M")

        # Calculate end time
        end_time = localized_dt + timedelta(minutes=LESSON_DURATION)

        bot.edit_message_text(
            f"✅ **Урок запланирован!**\n\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {localized_dt.strftime('%H:%M')}-{end_time.strftime('%H:%M')} (время учителя)\n"
            f"⏳ Длительность: {LESSON_DURATION} минут\n"
            f"👨‍🎓 Студент: {user_data['name']}\n"
            f"📊 Осталось уроков: {user_data['remaining']}",
            call.message.chat.id,
            call.message.message_id
        )

        # Notify teacher
        teacher_msg = (
            f"📥 **Новая запись на урок!**\n\n"
            f"👨‍🎓 Студент: {user_data['name']}\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {localized_dt.strftime('%H:%M')}-{end_time.strftime('%H:%M')}\n"
            f"⏳ Длительность: {LESSON_DURATION} минут\n"
            f"📞 Телефон: {user_data['phone'] or 'не указан'}"
        )
        if TEACHER_ID:
            bot.send_message(TEACHER_ID, teacher_msg, parse_mode="Markdown")

        # Schedule reminder
        schedule_reminder(lesson_id, proposed_datetime)

        bot.answer_callback_query(call.id, "✅ Урок успешно запланирован!")

    except Exception as e:
        print(f"Error booking slot: {e}")
        bot.answer_callback_query(call.id, "❌ Ошибка при записи на урок")

@bot.message_handler(func=lambda message: message.text == "📅 Мои записи" and not is_teacher(message.from_user.id))
def show_my_lessons(message):
    user_id = str(message.from_user.id)

    # Get user's lessons sorted by date
    user_lessons = sorted(
        [l for l in data['lessons'] if l['student_id'] == user_id and l.get('status') not in ['cancelled', 'cancelled_by_teacher']],
        key=lambda x: datetime.fromisoformat(x['datetime'])
    )

    if not user_lessons:
        bot.send_message(message.chat.id, "📭 У вас нет запланированных уроков.")
        return

    response = "📅 **Ваши запланированные уроки:**\n\n"
    for i, lesson in enumerate(user_lessons[:10]):
        dt = datetime.fromisoformat(lesson['datetime'])
        teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
        localized_dt = teacher_tz.localize(dt)

        # Format as DATE-WEEKDAY-TIME
        weekday_map = {
            0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ",
            4: "ПТ", 5: "СБ", 6: "ВС"
        }
        weekday = weekday_map[localized_dt.weekday()]
        date_str = localized_dt.strftime(f"%d.%m-{weekday}-%H:%M")

        # Convert to student's timezone for display
        student_dt, tz_name = convert_time_for_student(dt, data['users'][user_id]['timezone'])

        duration = lesson.get('duration', LESSON_DURATION)
        end_time = student_dt + timedelta(minutes=duration)

        response += f"{i+1}. {date_str}\n"
        response += f"   ⏰ Ваше время: {student_dt.strftime('%H:%M')}-{end_time.strftime('%H:%M')} ({tz_name})\n"
        response += f"   ⏳ Длительность: {duration} минут\n"
        response += f"   Статус: {lesson.get('status', 'scheduled')}\n\n"

    if len(user_lessons) > 10:
        response += f"\n...и еще {len(user_lessons) - 10} уроков"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "ℹ️ Осталось уроков" and not is_teacher(message.from_user.id))
def show_remaining_lessons(message):
    user_id = str(message.from_user.id)
    remaining = data['users'][user_id]['remaining']

    if remaining > 0:
        bot.send_message(
            message.chat.id,
            f"📊 **Осталось уроков:** {remaining}\n"
            f"⏳ **Длительность урока:** {LESSON_DURATION} минут\n\n"
            "Для пополнения свяжитесь с учителем (@CanEUHearMe).",
            parse_mode="Markdown"
        )
    else:
        bot.send_message(
            message.chat.id,
            "❌ **Уроки закончились**\n\nОбратитесь к учителю (@CanEUHearMe) для пополнения.",
            parse_mode="Markdown"
        )

@bot.message_handler(func=lambda message: message.text == "👨‍🏫 Админ-панель" and is_teacher(message.from_user.id))
def handle_admin_panel(message):
    admin_panel(message)

@bot.message_handler(func=lambda message: message.text == "👥 Список студентов" and is_teacher(message.from_user.id))
def show_students_list(message):
    students = []
    for user_id, user_data in data['users'].items():
        if user_id != TEACHER_ID:
            # Count upcoming lessons
            upcoming_count = 0
            for lesson_id in user_data['schedule']:
                lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)
                if lesson and lesson.get('status') not in ['cancelled', 'cancelled_by_teacher']:
                    lesson_dt = datetime.fromisoformat(lesson['datetime'])
                    if lesson_dt > datetime.now():
                        upcoming_count += 1

            students.append(f"👤 {user_data['name']} - {user_data['remaining']} уроков - {upcoming_count} запланировано")

    if students:
        response = "👥 **Список студентов:**\n\n" + "\n".join(students)
    else:
        response = "📭 Студентов пока нет"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "📊 Статистика" and is_teacher(message.from_user.id))
def show_statistics(message):
    """Show statistics for teacher"""
    total_students = len([uid for uid in data['users'] if uid != TEACHER_ID])

    # Count total lessons
    total_lessons = len(data['lessons'])

    # Count lessons by status
    scheduled_lessons = len([l for l in data['lessons'] if l.get('status') == 'scheduled'])
    cancelled_lessons = len([l for l in data['lessons'] if l.get('status') == 'cancelled'])
    cancelled_by_teacher_lessons = len([l for l in data['lessons'] if l.get('status') == 'cancelled_by_teacher'])
    reminded_lessons = len([l for l in data['lessons'] if l.get('status') == 'reminded'])

    # Count upcoming lessons
    upcoming_lessons = 0
    now = datetime.now()
    for lesson in data['lessons']:
        if lesson.get('status') == 'scheduled':
            lesson_dt = datetime.fromisoformat(lesson['datetime'])
            if lesson_dt > now:
                upcoming_lessons += 1

    # Calculate total lesson minutes
    total_minutes = 0
    for lesson in data['lessons']:
        if lesson.get('status') == 'scheduled':
            duration = lesson.get('duration', LESSON_DURATION)
            total_minutes += duration

    total_hours = total_minutes / 60

    # Count advertisements
    total_ads = len(data.get('advertisements', []))

    response = (
        f"📊 **Статистика бота**\n\n"
        f"👥 **Студенты:** {total_students}\n\n"
        f"📚 **Уроки:**\n"
        f"• Всего уроков: {total_lessons}\n"
        f"• Запланировано: {scheduled_lessons}\n"
        f"• Предстоящих: {upcoming_lessons}\n"
        f"• Отменено студентами: {cancelled_lessons}\n"
        f"• Отменено учителем: {cancelled_by_teacher_lessons}\n"
        f"• Напоминаний отправлено: {reminded_lessons}\n"
        f"• Всего часов: {total_hours:.1f} ч\n\n"
        f"📢 **Реклама:**\n"
        f"• Отправлено рассылок: {total_ads}\n\n"
        f"⚙️ **Настройки:**\n"
        f"• Длительность урока: {LESSON_DURATION} мин\n"
        f"• Перерыв между уроками: {BREAK_TIME} мин\n"
        f"• Часовой пояс учителя: {TIMEZONE_TEACHER}"
    )

    bot.send_message(message.chat.id, response, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "💰 Пополнить уроки" and is_teacher(message.from_user.id))
def request_student_for_topup(message):
    msg = bot.send_message(
        message.chat.id,
        "👤 **Пополнение уроков**\n\n"
        "Введите имя студента (как он указан в боте) для пополнения:",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_student_selection)

def process_student_selection(message):
    student_name = message.text.strip()

    # Find student
    student_id = None
    for uid, user_data in data['users'].items():
        if user_data['name'] == student_name:
            student_id = uid
            break

    if student_id:
        msg = bot.send_message(
            message.chat.id,
            f"✅ Найден студент: {student_name}\n\n"
            f"Текущий остаток: {data['users'][student_id]['remaining']}\n\n"
            "Введите количество уроков для добавления:",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, lambda m: process_topup(m, student_id, student_name))
    else:
        bot.send_message(message.chat.id, "❌ Студент не найден")

def process_topup(message, student_id, student_name):
    try:
        amount = int(message.text)
        if amount <= 0:
            raise ValueError

        data['users'][student_id]['remaining'] += amount
        save_data(data)

        bot.send_message(
            message.chat.id,
            f"✅ Уроки пополнены!\n\n"
            f"Студент: {student_name}\n"
            f"Добавлено уроков: {amount}\n"
            f"Новый остаток: {data['users'][student_id]['remaining']}",
            parse_mode="Markdown"
        )

        # Notify student
        bot.send_message(
            student_id,
            f"🎉 **Ваш баланс пополнен!**\n\n"
            f"Добавлено уроков: {amount}\n"
            f"Теперь у вас: {data['users'][student_id]['remaining']} уроков",
            parse_mode="Markdown"
        )

    except ValueError:
        bot.send_message(message.chat.id, "❌ Введите корректное число")

@bot.message_handler(func=lambda message: message.text == "📅 Текущее расписание" and is_teacher(message.from_user.id))
def show_current_timetable(message):
    if not data['teacher_timetable']:
        bot.send_message(message.chat.id, "📭 Расписание еще не установлено")
        return

    current_week = get_current_week_number()
    timetable_week = data.get('last_timetable_week', 'не установлена')

    response = f"📅 **Текущее расписание учителя:**\n\n"
    response += f"*Текущая неделя: {current_week}*\n"
    response += f"*Неделя расписания: {timetable_week}*\n"
    response += f"*Длительность урока: {LESSON_DURATION} минут*\n"
    response += f"*Перерыв между уроками: {BREAK_TIME} минут*\n\n"

    for entry in data['teacher_timetable']:
        response += f"• {entry['original']}\n"

    # Show if timetable is active for current week
    if is_timetable_for_current_week():
        response += f"\n✅ Это расписание активно на текущую неделю"
    else:
        response += f"\n❌ Это расписание устарело. Нужно создать новое на текущую неделю"

    bot.send_message(message.chat.id, response, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "🕐 Установить часовой пояс" and not is_teacher(message.from_user.id))
def request_timezone(message):
    markup = InlineKeyboardMarkup()
    timezones = [
        ("Москва (MSK)", "Europe/Moscow"),
        ("Екатеринбург", "Asia/Yekaterinburg"),
        ("Новосибирск", "Asia/Novosibirsk"),
        ("Владивосток", "Asia/Vladivostok"),
        ("Калининград", "Europe/Kaliningrad")
    ]

    for tz_name, tz_code in timezones:
        markup.add(InlineKeyboardButton(tz_name, callback_data=f"tz_{tz_code}"))

    bot.send_message(
        message.chat.id,
        "🌍 **Выберите ваш часовой пояс:**",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('tz_'))
def set_timezone(call):
    user_id = str(call.from_user.id)
    timezone = call.data[3:]  # Remove 'tz_' prefix

    data['users'][user_id]['timezone'] = timezone
    save_data(data)

    bot.edit_message_text(
        f"✅ Часовой пояс установлен: {timezone}",
        call.message.chat.id,
        call.message.message_id
    )
    bot.answer_callback_query(call.id)

# Reminder system
def schedule_reminder(lesson_id, lesson_datetime):
    """Schedule a reminder 15 minutes before lesson"""
    reminder_time = lesson_datetime - timedelta(minutes=15)
    if reminder_time > datetime.now():
        # In a real implementation, you would use a proper task scheduler
        # This is a simplified version
        threading.Thread(target=send_reminder, args=(lesson_id, reminder_time)).start()

def send_reminder(lesson_id, reminder_time):
    """Send reminder at specified time"""
    # Wait until reminder time
    wait_time = (reminder_time - datetime.now()).total_seconds()
    if wait_time > 0:
        time.sleep(wait_time)

    # Reload data
    global data
    data = load_data()

    # Find lesson
    lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)
    if lesson and lesson['status'] == 'scheduled':
        # Send reminder to student
        bot.send_message(
            lesson['student_id'],
            f"🔔 **Напоминание об уроке!**\n\n"
            f"Урок начнется через 15 минут!\n"
            f"Время: {datetime.fromisoformat(lesson['datetime']).strftime('%H:%M')}\n"
            f"Длительность: {lesson.get('duration', LESSON_DURATION)} минут",
            parse_mode="Markdown"
        )

        # Send reminder to teacher
        if TEACHER_ID:
            bot.send_message(
                TEACHER_ID,
                f"🔔 **Напоминание об уроке!**\n\n"
                f"Урок с {data['users'][lesson['student_id']]['name']} "
                f"начнется через 15 минут!\n"
                f"Длительность: {lesson.get('duration', LESSON_DURATION)} минут",
                parse_mode="Markdown"
            )

        # Mark as reminded
        lesson['status'] = 'reminded'
        save_data(data)

# Sunday timetable notification function
def send_weekly_timetable_to_students():
    """Send weekly timetable to all students on Sunday"""
    teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
    now = datetime.now(teacher_tz)

    # Check if it's Sunday (weekday 6) and morning (9:00-12:00)
    if now.weekday() == 6 and 9 <= now.hour <= 12:
        # Check if timetable is set for current week
        if not is_timetable_for_current_week():
            return  # No timetable set yet
        
        # Generate timetable message
        timetable_message = "📅 **Расписание на эту неделю:**\n\n"
        
        # Group slots by day
        slots_by_day = {}
        weekday_map = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
        weekday_names = {
            0: "Понедельник", 1: "Вторник", 2: "Среда", 
            3: "Четверг", 4: "Пятница", 5: "Суббота", 6: "Воскресенье"
        }
        
        for entry in data['teacher_timetable']:
            if entry.get('has_date'):
                date = entry.get('full_date')
                if date is None:
                    continue
                day_idx = date.weekday()
                day_name = weekday_names.get(day_idx, entry['day'])
                date_str = date.strftime("%d.%m")
            else:
                day_idx = weekday_map.index(entry['day']) if entry['day'] in weekday_map else -1
                if day_idx == -1:
                    continue
                day_name = weekday_names.get(day_idx, entry['day'])
                date = get_next_weekday(entry['day'], 0)
                date_str = date.strftime("%d.%m") if date else "N/A"
            
            if day_name not in slots_by_day:
                slots_by_day[day_name] = []
            
            slots_by_day[day_name].append(f"{entry['start']}-{entry['end']}")
        
        # Build message
        for day_name in ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]:
            if day_name in slots_by_day:
                times = ", ".join(slots_by_day[day_name])
                timetable_message += f"**{day_name}**: {times}\n"
        
        if len(slots_by_day) == 0:
            timetable_message += "Расписание еще не установлено.\n"
        else:
            timetable_message += "\n⏰ Время указано по Екатеринбургу.\n"
            timetable_message += "Используйте кнопку '🕐 Установить часовой пояс' для конвертации времени.\n"
            timetable_message += "\n📚 Записывайтесь на удобные слоты через бота!"
        
        # Send to all students
        for user_id, user_data in list(data['users'].items()):
            if user_id == TEACHER_ID:
                continue

            try:
                bot.send_message(
                    user_id,
                    timetable_message,
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Failed to send weekly timetable to {user_id}: {e}")

        # Send confirmation to teacher
        if TEACHER_ID:
            bot.send_message(
                TEACHER_ID,
                f"✅ Рассылка расписания на неделю выполнена!\n"
                f"Отправлено {len(data['users']) - 1} студентам.",
                parse_mode="Markdown"
            )

# Schedule the Sunday timetable notification
def schedule_sunday_timetable():
    """Schedule Sunday timetable check"""
    while True:
        try:
            send_weekly_timetable_to_students()
        except Exception as e:
            print(f"Error in Sunday timetable notification: {e}")

        # Check once per hour
        time.sleep(3600)

# Start Sunday timetable thread
sunday_thread = threading.Thread(target=schedule_sunday_timetable, daemon=True)
sunday_thread.start()

# Saturday evening reminder function
def send_saturday_timetable_reminder():
    """Send reminder on Saturday evening about timetable update"""
    teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
    now = datetime.now(teacher_tz)

    # Check if it's Saturday (weekday 5) and evening (18:00-22:00)
    if now.weekday() == 5 and 18 <= now.hour <= 22:
        # Send to all students
        for user_id, user_data in list(data['users'].items()):
            if user_id == TEACHER_ID:
                continue

            try:
                bot.send_message(
                    user_id,
                    "🔔 **Напоминание!**\n\n"
                    "Учитель скоро обновит расписание на следующую неделю!\n"
                    "Не забудьте записаться на уроки, когда расписание будет доступно.\n\n"
                    "Расписание обновляется каждую неделю в воскресенье.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                print(f"Failed to send Saturday reminder to {user_id}: {e}")

        # Send to teacher
        if TEACHER_ID:
            bot.send_message(
                TEACHER_ID,
                "👨‍🏫 **Напоминание об обновлении расписания!**\n\n"
                "Сегодня суббота - время обновить расписание на следующую неделю!\n"
                "Используйте кнопку '📝 Установить расписание' для создания расписания на следующую неделю.",
                parse_mode="Markdown"
            )

# Schedule the Saturday reminder
def schedule_saturday_reminder():
    """Schedule Saturday reminder check"""
    while True:
        try:
            send_saturday_timetable_reminder()
        except Exception as e:
            print(f"Error in Saturday reminder: {e}")

        # Check once per hour
        time.sleep(3600)

# Start Saturday reminder thread
saturday_thread = threading.Thread(target=schedule_saturday_reminder, daemon=True)
saturday_thread.start()

# Daily auto-schedule function
@bot.message_handler(func=lambda message: message.text == "📆 Авто-запись на каждый день" and not is_teacher(message.from_user.id))
def auto_schedule_daily(message):
    user_id = str(message.from_user.id)
    user_data = data['users'][user_id]

    if user_data['remaining'] <= 0:
        bot.send_message(message.chat.id, "❌ Недостаточно уроков для автозаписи")
        return

    # Find available slots for each day of current week
    lessons_to_schedule = min(user_data['remaining'], 7)  # Max 7 days

    scheduled_count = 0

    # Check if timetable is set for current week
    if not is_timetable_for_current_week():
        bot.send_message(
            message.chat.id,
            "❌ На эту неделю расписание еще не установлено учителем.\n"
            "Автозапись невозможна.",
            parse_mode="Markdown"
        )
        return

    for i in range(7):  # Next 7 days
        if scheduled_count >= lessons_to_schedule:
            break

        date = datetime.now() + timedelta(days=i+1)

        # Skip if not in current week
        date_week = date.isocalendar()[1]
        current_week = get_current_week_number()
        if date_week != current_week:
            continue

        # Find available slot for this day
        weekday_map = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
        weekday_ru = weekday_map[date.weekday()]

        # Look for teacher's slot on this weekday
        for entry in data['teacher_timetable']:
            if entry['day'] == weekday_ru or (entry.get('has_date') and entry.get('full_date').date() == date.date()):
                slots = create_time_slots(entry, date)
                for slot in slots:
                    if is_slot_available(slot['datetime']) and scheduled_count < lessons_to_schedule:
                        # Check for conflicts
                        conflict = False
                        for lesson_id in user_data['schedule']:
                            lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)
                            if lesson and lesson.get('status') not in ['cancelled', 'cancelled_by_teacher']:
                                lesson_start = datetime.fromisoformat(lesson['datetime'])
                                lesson_end = lesson_start + timedelta(minutes=LESSON_DURATION)
                                slot_end = slot['datetime'] + timedelta(minutes=LESSON_DURATION)

                                if not (slot_end <= lesson_start or slot['datetime'] >= lesson_end):
                                    conflict = True
                                    break

                        if not conflict:
                            # Book the slot
                            lesson_id = f"{date.strftime('%Y%m%d')}_{user_id}_{scheduled_count}"
                            lesson = {
                                "id": lesson_id,
                                "student_id": user_id,
                                "student_name": user_data['name'],
                                "datetime": slot['datetime'].isoformat(),
                                "teacher_timezone": TIMEZONE_TEACHER,
                                "status": "scheduled",
                                "duration": LESSON_DURATION
                            }

                            data['lessons'].append(lesson)
                            user_data['schedule'].append(lesson_id)
                            user_data['remaining'] -= 1
                            scheduled_count += 1

                            # Schedule reminder
                            schedule_reminder(lesson_id, slot['datetime'])
                            break
                if scheduled_count >= lessons_to_schedule:
                    break

    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ Автозапись выполнена!\n\n"
        f"Записано уроков: {scheduled_count}\n"
        f"Осталось уроков: {user_data['remaining']}",
        parse_mode="Markdown"
    )

# Request phone number
@bot.message_handler(func=lambda message: message.text == "📞 Указать телефон")
def request_phone(message):
    msg = bot.send_message(
        message.chat.id,
        "📱 **Укажите ваш номер телефона:**\n\n"
        "Формат: +7XXXXXXXXXX",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, save_phone)

def save_phone(message):
    user_id = str(message.from_user.id)
    phone = message.text.strip()

    # Simple phone validation
    if phone.startswith('+') and len(phone) >= 10:
        data['users'][user_id]['phone'] = phone
        save_data(data)
        bot.send_message(message.chat.id, "✅ Номер телефона сохранен!")
    else:
        bot.send_message(message.chat.id, "❌ Неверный формат номера")

# Update the show_menu function to include new buttons
@bot.message_handler(func=lambda message: message.text == "/menu" or message.text == "Меню")
def show_menu(message):
    if is_teacher(message.from_user.id):
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("📝 Установить расписание"))
        markup.add(KeyboardButton("👨‍🏫 Админ-панель"))
    else:
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("📚 Записаться на урок"))
        markup.add(KeyboardButton("📅 Мои записи"))
        markup.add(KeyboardButton("❌ Отменить запись"))
        markup.add(KeyboardButton("ℹ️ Осталось уроков"))
        markup.add(KeyboardButton("🕐 Установить часовой пояс"))
        markup.add(KeyboardButton("📞 Указать телефон"))
        markup.add(KeyboardButton("📆 Авто-запись на каждый день"))

    bot.send_message(
        message.chat.id,
        "📱 **Главное меню**",
        reply_markup=markup,
        parse_mode="Markdown"
    )

if __name__ == "__main__":
    print("=" * 50)
    print("Бот запущен...")
    print(f"Токен: {TOKEN[:10]}...")
    print(f"ID учителя: {TEACHER_ID}")
    print(f"Время перерыва: {BREAK_TIME} минут")
    print(f"Длительность урока: {LESSON_DURATION} минут")
    print("=" * 50)
    bot.polling(none_stop=True)
