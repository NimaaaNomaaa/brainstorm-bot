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
LESSON_LINK = "https://7kbsf14w.ktalk.ru/qns6bld7jqwq"  # Ссылка на занятие

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
        "last_timetable_week": None,  # Track which week the timetable is for
        "temp_state": {},  # Temporary state for multi-step booking
        "teacher_notes": {}  # Teacher's private notes for each student
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
        KeyboardButton("📝 Установить расписание"),
        KeyboardButton("📊 Статистика"),
        KeyboardButton("👥 Список студентов"),
        KeyboardButton("💰 Пополнить уроки"),
        KeyboardButton("📅 Текущее расписание"),
        KeyboardButton("❌ Удалить запись"),
        KeyboardButton("📢 Отправить рекламу"),
        KeyboardButton("📅 Календарь на неделю"),
        KeyboardButton("✏️ Заметки"),
        KeyboardButton("➕ Добавить занятие"),
        KeyboardButton("✏️ Изменить имя студента")
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
    is_new_user = False
    if user_id not in data['users']:
        is_new_user = True
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

    # If new student, give them 1 trial lesson and notify
    if is_new_user and not is_teacher(message.from_user.id):
        data['users'][user_id]['remaining'] = 1
        save_data(data)
        bot.send_message(
            message.chat.id,
            f"🎁 **Пробный урок!**\n\n"
            f"Вам добавлен 1 пробный урок.\n"
            f"Теперь у вас есть {data['users'][user_id]['remaining']} урок для записи.",
            parse_mode="Markdown"
        )
        # Notify teacher about new student
        if TEACHER_ID:
            bot.send_message(
                TEACHER_ID,
                f"🆕 **Новый студент!**\n\n"
                f"Имя: {user_name}\n"
                f"ID: {user_id}\n"
                f"Добавлен 1 пробный урок.",
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
            f"🔄 Урок возвращен студенту\n"
            f"🔗 Ссылка на занятие: {LESSON_LINK}",
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
            f"📊 Осталось уроков: {data['users'][student_id]['remaining']}\n"
            f"🔗 Ссылка на занятие: {LESSON_LINK}",
            parse_mode="Markdown"
        )
    except:
        pass  # Student may have blocked the bot

    # Notify teacher in chat about cancellation
    bot.send_message(
        TEACHER_ID,
        f"❌ **Урок отменен преподавателем**\n\n"
        f"👨‍🎓 Студент: {lesson['student_name']}\n"
        f"📅 Дата: {date_str}\n"
        f"🔄 Урок возвращен студенту\n"
        f"🔗 Ссылка на занятие: {LESSON_LINK}",
        parse_mode="Markdown"
    )

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

        # Send to all students (forwarding not possible with captions, so send as new message)
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
    # Don't clear previous timetable - allow editing
    # data['teacher_timetable'] = []

    msg = bot.send_message(
        message.chat.id,
        f"📅 **Установите расписание**\n\n"
        f"Текущее время перерыва: {BREAK_TIME} минут\n"
        f"Длительность урока: {LESSON_DURATION} минут\n\n"
        f"Отправьте расписание в формате:\n"
        f"`ПН - 18:00-20:00`\n"
        f"`СР - 19:00-21:00`\n\n"
        f"Или используйте формат с датами:\n"
        f"`15.12-ПН-18:00-20:00`\n\n"
        f"Можно указать несколько строк. Отправьте 'готово' когда закончите.\n"
        f"Или отправьте **'оставить'** чтобы сохранить текущее расписание без изменений.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, process_timetable)

def process_timetable(message):
    if message.text.lower() == 'оставить':
        bot.send_message(
            message.chat.id,
            "✅ **Расписание сохранено без изменений.**\n\n"
            f"Текущее расписание:\n" + 
            "\n".join([f"• {e['original']}" for e in data['teacher_timetable']]) if data['teacher_timetable'] else "❌ Расписание пустое",
            parse_mode="Markdown"
        )
        admin_panel(message)
        return

    if message.text.lower() == 'готово':
        if not data['teacher_timetable']:
            bot.send_message(message.chat.id, "❌ Расписание пустое. Отправьте хотя бы один элемент.")
            return

        # Set current week number
        data['last_timetable_week'] = get_current_week_number()

        # Save the timetable
        save_data(data)

        # Generate and send available slots for current week
        available_slots = []
        for entry in data['teacher_timetable']:
            if entry.get('has_date'):
                date = entry.get('full_date')
                if date and date.isocalendar()[1] == data['last_timetable_week']:
                    available_slots.extend(create_time_slots(entry, date))
            else:
                date = get_next_weekday(entry['day'], 0)
                if date:
                    available_slots.extend(create_time_slots(entry, date))

        # Format and send summary
        summary = "✅ **Расписание установлено!**\n\n"
        summary += f"📅 Неделя: {data['last_timetable_week']}-я неделя года\n\n"
        summary += "**Ваше расписание:**\n"
        for entry in data['teacher_timetable']:
            summary += f"• {entry['original']}\n"

        summary += f"\n📊 Доступные слоты на текущую неделю: {len(available_slots)}\n"
        summary += f"⏰ Уроки: {LESSON_DURATION} мин, перерывы: {BREAK_TIME} мин"

        bot.send_message(message.chat.id, summary, parse_mode="Markdown")

        # Also send to admin panel
        admin_panel(message)
        return

    # Parse and add new entries
    entries = message.text.strip().split('\n')
    added_entries = []
    failed_entries = []

    for entry_text in entries:
        entry_text = entry_text.strip()
        if not entry_text:
            continue

        parsed_entry = parse_timetable_entry(entry_text)
        data['teacher_timetable'].append(parsed_entry)

        if parsed_entry['day'] == 'UNKNOWN':
            failed_entries.append(entry_text)
        else:
            added_entries.append(f"{parsed_entry['original']} → {parsed_entry['day']} {parsed_entry['start']}-{parsed_entry['end']}")

    # Save after adding entries
    save_data(data)

    # Send feedback
    feedback = ""
    if added_entries:
        feedback += "✅ Добавлены:\n"
        for entry in added_entries:
            feedback += f"• {entry}\n"
    if failed_entries:
        feedback += "\n❌ Не удалось распознать:\n"
        for entry in failed_entries:
            feedback += f"• {entry}\n"

    feedback += "\nПродолжайте отправлять строки или напишите 'готово'."

    bot.send_message(message.chat.id, feedback)

    # Continue waiting for more entries
    bot.register_next_step_handler(message, process_timetable)

@bot.message_handler(func=lambda message: message.text == "📊 Статистика" and is_teacher(message.from_user.id))
def show_statistics(message):
    total_students = len([uid for uid in data['users'] if uid != TEACHER_ID])
    total_lessons = len(data['lessons'])
    upcoming_lessons = sum(1 for lesson in data['lessons'] if datetime.fromisoformat(lesson['datetime']) > datetime.now() and lesson.get('status') not in ['cancelled', 'cancelled_by_teacher'])

    completed_lessons = total_lessons - upcoming_lessons

    # Calculate total remaining lessons for all students
    total_remaining = sum(user['remaining'] for user in data['users'].values() if 'remaining' in user)

    stats_text = f"""
📈 **Статистика бота**

👥 Всего студентов: {total_students}
📚 Всего уроков в системе: {total_lessons}
✅ Завершенных уроков: {completed_lessons}
⏳ Предстоящих уроков: {upcoming_lessons}
💰 Неиспользованных уроков студентами: {total_remaining}

⏰ Длительность урока: {LESSON_DURATION} мин
⏸️ Время перерыва: {BREAK_TIME} мин
"""

    bot.send_message(message.chat.id, stats_text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "👥 Список студентов" and is_teacher(message.from_user.id))
def show_students_list(message):
    students = [user for uid, user in data['users'].items() if uid != TEACHER_ID]

    if not students:
        bot.send_message(message.chat.id, "📭 Нет зарегистрированных студентов.")
        return

    students_list = "👥 **Список студентов:**\n\n"
    for i, user in enumerate(students, 1):
        students_list += f"{i}. {user['name']}\n"
        students_list += f"   Уроков осталось: {user.get('remaining', 0)}\n"
        if user.get('phone'):
            students_list += f"   Телефон: {user['phone']}\n"
        students_list += f"   Часовой пояс: {user.get('timezone', 'Europe/Moscow')}\n\n"

    # Add pagination or limit if too many students
    if len(students_list.encode('utf-8')) > 4096:  # Telegram message limit
        students_list = students_list[:4000] + "\n... (и другие)"

    bot.send_message(message.chat.id, students_list, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "💰 Пополнить уроки" and is_teacher(message.from_user.id))
def request_student_for_adding_lessons(message):
    students = [user for uid, user in data['users'].items() if uid != TEACHER_ID]

    if not students:
        bot.send_message(message.chat.id, "📭 Нет зарегистрированных студентов.")
        return

    markup = InlineKeyboardMarkup()
    for user in students:
        user_id = [uid for uid, u in data['users'].items() if u == user][0]
        markup.add(InlineKeyboardButton(
            f"{user['name']} (осталось: {user.get('remaining', 0)})",
            callback_data=f"add_lessons_select_{user_id}"
        ))

    bot.send_message(
        message.chat.id,
        "💰 **Выберите студента для пополнения уроков:**",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('add_lessons_select_'))
def select_student_for_adding_lessons(call):
    user_id = call.data.replace('add_lessons_select_', '')
    student = data['users'][user_id]

    msg = bot.send_message(
        call.message.chat.id,
        f"💰 **Пополнение уроков для {student['name']}**\n\n"
        f"Текущий баланс: {student.get('remaining', 0)} уроков\n\n"
        f"Введите количество уроков для добавления:"
    )
    bot.register_next_step_handler(msg, lambda m: process_add_lessons(m, user_id))

def process_add_lessons(message, user_id):
    try:
        amount = int(message.text)
        if amount <= 0:
            bot.send_message(message.chat.id, "❌ Количество должно быть положительным числом.")
            return

        data['users'][user_id]['remaining'] = data['users'][user_id].get('remaining', 0) + amount
        save_data(data)

        student = data['users'][user_id]
        bot.send_message(
            message.chat.id,
            f"✅ **Успешно!**\n\n"
            f"Студент: {student['name']}\n"
            f"Добавлено: {amount} уроков\n"
            f"Теперь у него: {student['remaining']} уроков"
        )

        # Notify student
        try:
            bot.send_message(
                user_id,
                f"💰 **Вам добавили уроки!**\n\n"
                f"Добавлено: {amount} уроков\n"
                f"Теперь у вас: {student['remaining']} уроков",
                parse_mode="Markdown"
            )
        except:
            pass  # Student may have blocked the bot

    except ValueError:
        bot.send_message(message.chat.id, "❌ Пожалуйста, введите корректное число.")

@bot.message_handler(func=lambda message: message.text == "📅 Текущее расписание" and is_teacher(message.from_user.id))
def show_current_timetable(message):
    if not data['teacher_timetable']:
        bot.send_message(message.chat.id, "📭 Расписание не установлено.")
        return

    timetable_text = "📅 **Текущее расписание:**\n\n"
    for i, entry in enumerate(data['teacher_timetable'], 1):
        timetable_text += f"{i}. {entry['original']}\n"

    timetable_text += f"\n⏰ Уроки: {LESSON_DURATION} мин, перерывы: {BREAK_TIME} мин\n"
    if data.get('last_timetable_week'):
        timetable_text += f"🗓️ Для недели: {data['last_timetable_week']}-й неделя года"

    bot.send_message(message.chat.id, timetable_text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "📚 Записаться на урок" and not is_teacher(message.from_user.id))
def book_lesson_student(message):
    user_id = str(message.from_user.id)

    # Check if student has remaining lessons
    if data['users'][user_id]['remaining'] <= 0:
        bot.send_message(
            message.chat.id,
            "❌ У вас закончились уроки. Попросите учителя пополнить ваш баланс."
        )
        return

    # Check if timetable is set for current week
    if not is_timetable_for_current_week():
        bot.send_message(
            message.chat.id,
            "❌ Расписание на эту неделю еще не установлено. Попробуйте позже."
        )
        return

    # Get available slots
    available_slots = get_available_slots_for_user(user_id)

    if not available_slots:
        bot.send_message(
            message.chat.id,
            "❌ Нет доступных слотов для записи на эту неделю. Попробуйте позже."
        )
        return

    # Create inline keyboard with available slots
    markup = InlineKeyboardMarkup()
    for i, slot in enumerate(available_slots[:25]):  # Limit to 25 slots
        button_text = f"{slot['date_str']} - {slot['student_time']} ({slot['timezone']})"
        callback_data = f"book_{i}_{slot['day']}_{slot['datetime'].strftime('%Y%m%d_%H%M')}"
        markup.add(InlineKeyboardButton(button_text, callback_data=callback_data))

    # Add "Обновить" button
    markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_slots"))

    bot.send_message(
        message.chat.id,
        f"📚 **Доступные слоты (осталось уроков: {data['users'][user_id]['remaining']}):**\n\n"
        f"Выберите удобное время для урока:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('book_'))
def process_booking(call):
    user_id = str(call.from_user.id)

    # Check if student has remaining lessons
    if data['users'][user_id]['remaining'] <= 0:
        bot.answer_callback_query(
            call.id,
            "❌ У вас закончились урокы. Попросите учителя пополнить ваш баланс."
        )
        return

    # Parse callback data
    data_parts = call.data.replace('book_', '').split('_')
    if len(data_parts) < 3:
        bot.answer_callback_query(call.id, "❌ Ошибка в данных")
        return

    day = data_parts[1]
    datetime_str = f"{data_parts[2]}_{data_parts[3]}"

    try:
        proposed_datetime = datetime.strptime(datetime_str, "%Y%m%d_%H%M")

        # Check if slot is still available
        if not is_slot_available(proposed_datetime):
            bot.answer_callback_query(call.id, "❌ Этот слот уже занят. Выберите другой.")
            return

        # Create lesson object
        lesson_id = f"lesson_{user_id}_{int(proposed_datetime.timestamp())}"

        lesson = {
            "id": lesson_id,
            "student_id": user_id,
            "student_name": data['users'][user_id]['name'],
            "datetime": proposed_datetime.isoformat(),
            "status": "booked"
        }

        # Add lesson to data
        data['lessons'].append(lesson)

        # Deduct lesson from student
        data['users'][user_id]['remaining'] -= 1
        data['users'][user_id]['schedule'].append(lesson_id)

        # Save data
        save_data(data)

        # Format confirmation message
        teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
        localized_time = teacher_tz.localize(proposed_datetime)
        weekday_map = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}
        weekday = weekday_map[localized_time.weekday()]
        date_str = localized_time.strftime(f"%d.%m-{weekday}-%H:%M")
        end_time = localized_time + timedelta(minutes=LESSON_DURATION)

        bot.edit_message_text(
            f"✅ **Вы записались на урок!**\n\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {localized_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')} (время учителя)\n"
            f"📊 Осталось уроков: {data['users'][user_id]['remaining']}\n"
            f"📱 Ваше время: {convert_time_for_student(proposed_datetime, data['users'][user_id]['timezone'])[0].strftime('%H:%M')} ({convert_time_for_student(proposed_datetime, data['users'][user_id]['timezone'])[1]})",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="Markdown"
        )

        # Notify teacher
        if TEACHER_ID:
            bot.send_message(
                TEACHER_ID,
                f"📚 **Новая запись**\n\n"
                f"👨‍🎓 Студент: {data['users'][user_id]['name']}\n"
                f"📅 Дата: {date_str}\n"
                f"⏰ Время: {localized_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}\n"
                f"📱 Время студента: {convert_time_for_student(proposed_datetime, data['users'][user_id]['timezone'])[0].strftime('%H:%M')} ({convert_time_for_student(proposed_datetime, data['users'][user_id]['timezone'])[1]})",
                parse_mode="Markdown"
            )

        # Schedule reminder
        schedule_reminder(lesson_id, proposed_datetime)

    except Exception as e:
        print(f"Error in booking: {e}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при записи")

@bot.callback_query_handler(func=lambda call: call.data == "refresh_slots")
def refresh_slots(call):
    """Refresh available slots for booking"""
    user_id = str(call.from_user.id)

    # Check if student has remaining lessons
    if data['users'][user_id]['remaining'] <= 0:
        bot.answer_callback_query(
            call.id,
            "❌ У вас закончились урокы. Попросите учителя пополнить ваш баланс."
        )
        return

    # Check if timetable is set for current week
    if not is_timetable_for_current_week():
        bot.answer_callback_query(
            call.id,
            "❌ Расписание на эту неделю еще не установлено."
        )
        return

    # Get updated available slots
    available_slots = get_available_slots_for_user(user_id)

    if not available_slots:
        bot.edit_message_text(
            "❌ Нет доступных слотов для записи на эту неделю.",
            call.message.chat.id,
            call.message.message_id
        )
        return

    # Create updated inline keyboard with available slots
    markup = InlineKeyboardMarkup()
    for i, slot in enumerate(available_slots[:25]):  # Limit to 25 slots
        button_text = f"{slot['date_str']} - {slot['student_time']} ({slot['timezone']})"
        callback_data = f"book_{i}_{slot['day']}_{slot['datetime'].strftime('%Y%m%d_%H%M')}"
        markup.add(InlineKeyboardButton(button_text, callback_data=callback_data))

    # Add "Обновить" button
    markup.add(InlineKeyboardButton("🔄 Обновить", callback_data="refresh_slots"))

    bot.edit_message_text(
        f"📚 **Доступные слоты (осталось уроков: {data['users'][user_id]['remaining']}):**\n\n"
        f"Выберите удобное время для урока:",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup
    )

def schedule_reminder(lesson_id, lesson_datetime):
    """Schedule reminder 15 minutes before lesson"""
    reminder_time = lesson_datetime - timedelta(minutes=15)

    def send_reminder():
        # Reload data to ensure we have latest state
        global data
        data = load_data()

        lesson = next((l for l in data['lessons'] if l['id'] == lesson_id), None)
        if not lesson or lesson.get('status') in ['cancelled', 'cancelled_by_teacher']:
            return

        # Send reminder to student
        try:
            dt = datetime.fromisoformat(lesson['datetime'])
            date_str = dt.strftime("%d.%m.%Y %H:%M")
            bot.send_message(
                lesson['student_id'],
                f"🔔 **Напоминание об уроке!**\n\n"
                f"📅 Дата: {date_str}\n"
                f"👨‍🎓 Студент: {lesson['student_name']}\n"
                f"🔗 Ссылка на занятие: {LESSON_LINK}",
                parse_mode="Markdown"
            )
        except Exception as e:
            print(f"Error sending reminder to student: {e}")
            pass  # Student may have blocked the bot

        # Send reminder to teacher
        try:
            if TEACHER_ID:
                bot.send_message(
                    TEACHER_ID,
                    f"🔔 **Напоминание: урок через 15 минут!**\n\n"
                    f"📅 Дата: {date_str}\n"
                    f"👨‍🎓 Студент: {lesson['student_name']}\n"
                    f"🔗 Ссылка на занятие: {LESSON_LINK}",
                    parse_mode="Markdown"
                )
        except Exception as e:
            print(f"Error sending reminder to teacher: {e}")
            pass

    # Schedule the reminder using a one-time scheduler approach
    def check_and_send():
        now = datetime.now()
        if now >= reminder_time:
            send_reminder()
            return False  # Stop repeating
        return True  # Keep checking

    # Use a thread to wait until the reminder time
    def wait_and_send():
        while datetime.now() < reminder_time:
            time.sleep(30)  # Check every 30 seconds
        send_reminder()

    reminder_thread = threading.Thread(target=wait_and_send, daemon=True)
    reminder_thread.start()

def run_scheduler():
    """Run the scheduler in a separate thread"""
    while True:
        schedule.run_pending()
        time.sleep(60)  # Check every minute

# Start scheduler thread
scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
scheduler_thread.start()

@bot.message_handler(func=lambda message: message.text == "📅 Мои записи" and not is_teacher(message.from_user.id))
def show_my_bookings(message):
    user_id = str(message.from_user.id)

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

    # Sort lessons by date
    upcoming_lessons.sort(key=lambda x: datetime.fromisoformat(x['datetime']))

    bookings_text = "📅 **Ваши предстоящие уроки:**\n\n"
    for i, lesson in enumerate(upcoming_lessons, 1):
        dt = datetime.fromisoformat(lesson['datetime'])
        date_str = dt.strftime("%d.%m-%a-%H:%M").replace("Mon", "ПН").replace("Tue", "ВТ").replace("Wed", "СР")\
                   .replace("Thu", "ЧТ").replace("Fri", "ПТ").replace("Sat", "СБ").replace("Sun", "ВС")
        bookings_text += f"{i}. {date_str}\n"

    bookings_text += f"\n📊 Осталось уроков: {data['users'][user_id]['remaining']}"

    bot.send_message(message.chat.id, bookings_text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "ℹ️ Осталось уроков" and not is_teacher(message.from_user.id))
def show_remaining_lessons(message):
    user_id = str(message.from_user.id)
    remaining = data['users'][user_id]['remaining']
    bot.send_message(
        message.chat.id,
        f"💰 **Осталось уроков:** {remaining}\n\n"
        f"Если нужно больше уроков - свяжитесь с преподавателем."
    )

@bot.message_handler(func=lambda message: message.text == "🕐 Установить часовой пояс" and not is_teacher(message.from_user.id))
def request_timezone(message):
    user_id = str(message.from_user.id)

    # Common timezones for selection
    timezones = [
        ("MSK", "Europe/Moscow"),
        ("SPB", "Europe/Moscow"),
        ("ЕКБ", "Asia/Yekaterinburg"),
        ("НСК", "Asia/Novosibirsk"),
        ("Красноярск", "Asia/Krasnoyarsk"),
        ("Хабаровск", "Asia/Vladivostok"),
        ("Самара", "Europe/Samara"),
        ("Калининград", "Europe/Kaliningrad")
    ]

    markup = InlineKeyboardMarkup()
    for name, tz in timezones:
        markup.add(InlineKeyboardButton(f"{name} - {tz}", callback_data=f"settz_{tz}"))

    # Add custom option
    markup.add(InlineKeyboardButton("✏️ Другой часовой пояс", callback_data="settz_custom"))

    bot.send_message(
        message.chat.id,
        f"🕐 **Установите часовой пояс**\n\n"
        f"Текущий: {data['users'][user_id].get('timezone', 'Europe/Moscow')}\n\n"
        f"Выберите из списка или укажите свой:",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('settz_'))
def process_timezone_selection(call):
    user_id = str(call.from_user.id)
    tz_data = call.data.replace('settz_', '')

    if tz_data == "custom":
        msg = bot.send_message(
            call.message.chat.id,
            "🕐 **Введите часовой пояс вручную**\n\n"
            "Пример: Europe/Moscow, Asia/Yekaterinburg, US/Eastern\n"
            "Или отправьте 'отмена' для отмены."
        )
        bot.register_next_step_handler(msg, lambda m: process_custom_timezone(m, user_id))
    else:
        # Set selected timezone
        data['users'][user_id]['timezone'] = tz_data
        save_data(data)

        bot.edit_message_text(
            f"✅ **Часовой пояс изменен!**\n\n"
            f"Теперь используется: {tz_data}",
            call.message.chat.id,
            call.message.message_id
        )

def process_custom_timezone(message, user_id):
    if message.text.lower() == 'отмена':
        bot.send_message(message.chat.id, "❌ Изменение часового пояса отменено.")
        return

    # Validate timezone
    try:
        pytz.timezone(message.text)
        data['users'][user_id]['timezone'] = message.text
        save_data(data)

        bot.send_message(
            message.chat.id,
            f"✅ **Часовой пояс изменен!**\n\n"
            f"Теперь используется: {message.text}"
        )
    except:
        bot.send_message(
            message.chat.id,
            "❌ Неверный формат часового пояса. Попробуйте еще раз.\n"
            "Пример: Europe/Moscow, Asia/Yekaterinburg"
        )

@bot.message_handler(func=lambda message: message.text == "📞 Указать телефон" and not is_teacher(message.from_user.id))
def request_phone(message):
    user_id = str(message.from_user.id)

    msg = bot.send_message(
        message.chat.id,
        "📞 **Укажите ваш номер телефона**\n\n"
        "Это может потребоваться преподавателю для связи.\n\n"
        "Отправьте номер или 'отмена' для отмены."
    )
    bot.register_next_step_handler(msg, lambda m: process_phone(m, user_id))

def process_phone(message, user_id):
    if message.text.lower() == 'отмена':
        bot.send_message(message.chat.id, "❌ Указание телефона отменено.")
        return

    # Store phone number
    data['users'][user_id]['phone'] = message.text
    save_data(data)

    bot.send_message(
        message.chat.id,
        f"✅ **Телефон сохранен!**\n\n"
        f"Ваш номер: {message.text}\n\n"
        f"Теперь преподаватель может связаться с вами при необходимости."
    )

@bot.message_handler(func=lambda message: message.text == "👨‍🏫 Админ-панель" and is_teacher(message.from_user.id))
def admin_panel_handler(message):
    admin_panel(message)

# Calendar for the week
@bot.message_handler(func=lambda message: message.text == "📅 Календарь на неделю" and is_teacher(message.from_user.id))
def show_weekly_calendar(message):
    """Show weekly calendar with all lessons"""
    now = datetime.now(pytz.timezone(TIMEZONE_TEACHER))
    # Get start of current week (Monday)
    start_of_week = now - timedelta(days=now.weekday())
    start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
    
    weekday_names = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    calendar_text = "📅 **Календарь на эту неделю**\n\n"
    
    for day_offset in range(7):
        current_day = start_of_week + timedelta(days=day_offset)
        day_name = weekday_names[day_offset]
        date_str = current_day.strftime("%d.%m")
        
        calendar_text += f"**{day_name} ({date_str}):**\n"
        
        # Find all lessons for this day
        day_lessons = []
        for lesson in data['lessons']:
            if lesson.get('status') in ['cancelled', 'cancelled_by_teacher']:
                continue
            lesson_dt = datetime.fromisoformat(lesson['datetime'])
            lesson_date = lesson_dt.date()
            if lesson_date == current_day.date():
                day_lessons.append((lesson_dt, lesson))
        
        if day_lessons:
            day_lessons.sort(key=lambda x: x[0])
            for lesson_dt, lesson in day_lessons:
                time_str = lesson_dt.strftime("%H:%M")
                calendar_text += f"  • {time_str} - {lesson['student_name']}\n"
        else:
            calendar_text += "  • Нет занятий\n"
        calendar_text += "\n"
    
    bot.send_message(message.chat.id, calendar_text, parse_mode="Markdown")

# Teacher notes management
@bot.message_handler(func=lambda message: message.text == "✏️ Заметки" and is_teacher(message.from_user.id))
def manage_notes(message):
    """Manage teacher's private notes for students"""
    students = [user for uid, user in data['users'].items() if uid != TEACHER_ID]
    
    if not students:
        bot.send_message(message.chat.id, "📭 Нет зарегистрированных студентов.")
        return
    
    markup = InlineKeyboardMarkup()
    for user in students:
        user_id = [uid for uid, u in data['users'].items() if u == user][0]
        has_note = user_id in data.get('teacher_notes', {}) and data['teacher_notes'][user_id]
        note_indicator = "📝" if has_note else ""
        markup.add(InlineKeyboardButton(
            f"{note_indicator} {user['name']}",
            callback_data=f"note_edit_{user_id}"
        ))
    
    bot.send_message(
        message.chat.id,
        "✏️ **Заметки преподавателя**\n\nВыберите студента для просмотра/редактирования заметки:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('note_edit_'))
def edit_note(call):
    """Edit or view note for a student"""
    user_id = call.data.replace('note_edit_', '')
    student = data['users'].get(user_id)
    
    if not student:
        bot.answer_callback_query(call.id, "❌ Студент не найден")
        return
    
    current_note = data.get('teacher_notes', {}).get(user_id, "")
    
    msg = bot.send_message(
        call.message.chat.id,
        f"✏️ **Заметка для {student['name']}**\n\n"
        f"Текущая заметка:\n{current_note if current_note else '*(пусто)*'}\n\n"
        f"Отправьте новый текст заметки или 'удалить' чтобы очистить.",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, lambda m: save_note(m, user_id, student['name']))
    bot.answer_callback_query(call.id)

def save_note(message, user_id, student_name):
    """Save teacher's note for a student"""
    if message.text.lower() == 'удалить':
        if 'teacher_notes' not in data:
            data['teacher_notes'] = {}
        data['teacher_notes'][user_id] = ""
        save_data(data)
        bot.send_message(message.chat.id, f"✅ Заметка для {student_name} удалена.")
        return
    
    if 'teacher_notes' not in data:
        data['teacher_notes'] = {}
    data['teacher_notes'][user_id] = message.text
    save_data(data)
    
    bot.send_message(
        message.chat.id,
        f"✅ **Заметка сохранена!**\n\n"
        f"Студент: {student_name}\n"
        f"Заметка: {message.text}",
        parse_mode="Markdown"
    )

# Add lesson for student (for students without Telegram)
@bot.message_handler(func=lambda message: message.text == "➕ Добавить занятие" and is_teacher(message.from_user.id))
def add_lesson_for_student(message):
    """Add a lesson for a student manually"""
    students = [user for uid, user in data['users'].items() if uid != TEACHER_ID]
    
    if not students:
        bot.send_message(message.chat.id, "📭 Нет зарегистрированных студентов.")
        return
    
    markup = InlineKeyboardMarkup()
    for user in students:
        user_id = [uid for uid, u in data['users'].items() if u == user][0]
        markup.add(InlineKeyboardButton(
            f"{user['name']} (осталось: {user.get('remaining', 0)})",
            callback_data=f"add_lesson_select_{user_id}"
        ))
    
    bot.send_message(
        message.chat.id,
        "➕ **Добавить занятие**\n\nВыберите студента:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('add_lesson_select_'))
def select_student_for_lesson(call):
    """Select student and ask for lesson datetime"""
    user_id = call.data.replace('add_lesson_select_', '')
    student = data['users'].get(user_id)
    
    if not student:
        bot.answer_callback_query(call.id, "❌ Студент не найден")
        return
    
    # Store state for adding lesson
    if 'temp_state' not in data:
        data['temp_state'] = {}
    data['temp_state'][user_id] = {'action': 'add_lesson', 'student_id': user_id}
    save_data(data)
    
    msg = bot.send_message(
        call.message.chat.id,
        f"➕ **Добавление занятия для {student['name']}**\n\n"
        f"Отправьте дату и время в формате:\n"
        f"`ДД.ММ ЧЧ:ММ`\n\n"
        f"Например: `25.12 18:00`",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, lambda m: process_add_lesson_datetime(m, user_id))
    bot.answer_callback_query(call.id)

def process_add_lesson_datetime(message, user_id):
    """Process datetime for adding lesson"""
    try:
        # Parse datetime in format DD.MM HH:MM
        pattern = r'(\d{1,2})\.(\d{1,2})\s+(\d{1,2}):(\d{2})'
        match = re.match(pattern, message.text.strip())
        
        if not match:
            bot.send_message(message.chat.id, "❌ Неверный формат. Используйте ДД.ММ ЧЧ:ММ")
            return
        
        day, month, hour, minute = map(int, match.groups())
        current_year = datetime.now().year
        
        proposed_datetime = datetime(current_year, month, day, hour, minute)
        
        # Check if slot is available
        if not is_slot_available(proposed_datetime):
            bot.send_message(message.chat.id, "❌ Это время уже занято. Выберите другое.")
            return
        
        # Create lesson
        lesson_id = f"lesson_{user_id}_{int(proposed_datetime.timestamp())}"
        student = data['users'][user_id]
        
        lesson = {
            "id": lesson_id,
            "student_id": user_id,
            "student_name": student['name'],
            "datetime": proposed_datetime.isoformat(),
            "status": "booked"
        }
        
        data['lessons'].append(lesson)
        data['users'][user_id]['remaining'] -= 1
        data['users'][user_id]['schedule'].append(lesson_id)
        save_data(data)
        
        # Format confirmation
        teacher_tz = pytz.timezone(TIMEZONE_TEACHER)
        localized_time = teacher_tz.localize(proposed_datetime)
        weekday_map = {0: "ПН", 1: "ВТ", 2: "СР", 3: "ЧТ", 4: "ПТ", 5: "СБ", 6: "ВС"}
        weekday = weekday_map[localized_time.weekday()]
        date_str = localized_time.strftime(f"%d.%m-{weekday}-%H:%M")
        end_time = localized_time + timedelta(minutes=LESSON_DURATION)
        
        bot.send_message(
            message.chat.id,
            f"✅ **Занятие добавлено!**\n\n"
            f"👨‍🎓 Студент: {student['name']}\n"
            f"📅 Дата: {date_str}\n"
            f"⏰ Время: {localized_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')}\n"
            f"🔗 Ссылка: {LESSON_LINK}\n"
            f"📊 Осталось уроков у студента: {data['users'][user_id]['remaining']}",
            parse_mode="Markdown"
        )
        
        # Schedule reminder
        schedule_reminder(lesson_id, proposed_datetime)
        
    except ValueError as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")

# Change student name
@bot.message_handler(func=lambda message: message.text == "✏️ Изменить имя студента" and is_teacher(message.from_user.id))
def change_student_name(message):
    """Change student's name in the system"""
    students = [user for uid, user in data['users'].items() if uid != TEACHER_ID]
    
    if not students:
        bot.send_message(message.chat.id, "📭 Нет зарегистрированных студентов.")
        return
    
    markup = InlineKeyboardMarkup()
    for user in students:
        user_id = [uid for uid, u in data['users'].items() if u == user][0]
        markup.add(InlineKeyboardButton(
            f"{user['name']}",
            callback_data=f"rename_select_{user_id}"
        ))
    
    bot.send_message(
        message.chat.id,
        "✏️ **Изменение имени студента**\n\nВыберите студента:",
        reply_markup=markup,
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('rename_select_'))
def select_student_for_rename(call):
    """Select student and ask for new name"""
    user_id = call.data.replace('rename_select_', '')
    student = data['users'].get(user_id)
    
    if not student:
        bot.answer_callback_query(call.id, "❌ Студент не найден")
        return
    
    # Store state for renaming
    if 'temp_state' not in data:
        data['temp_state'] = {}
    data['temp_state'][user_id] = {'action': 'rename', 'student_id': user_id}
    save_data(data)
    
    msg = bot.send_message(
        call.message.chat.id,
        f"✏️ **Изменение имени для {student['name']}**\n\n"
        f"Отправьте новое имя студента:",
        parse_mode="Markdown"
    )
    bot.register_next_step_handler(msg, lambda m: process_rename(m, user_id))
    bot.answer_callback_query(call.id)

def process_rename(message, user_id):
    """Process new name for student"""
    if not message.text.strip():
        bot.send_message(message.chat.id, "❌ Имя не может быть пустым.")
        return
    
    student = data['users'][user_id]
    old_name = student['name']
    student['name'] = message.text.strip()
    save_data(data)
    
    # Update student name in all their lessons
    for lesson in data['lessons']:
        if lesson['student_id'] == user_id:
            lesson['student_name'] = student['name']
    save_data(data)
    
    bot.send_message(
        message.chat.id,
        f"✅ **Имя изменено!**\n\n"
        f"Старое имя: {old_name}\n"
        f"Новое имя: {student['name']}",
        parse_mode="Markdown"
    )

print("🤖 Бот запущен...")
bot.infinity_polling()