import flet as ft
import requests
import folium
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import os
import json
import threading
import re
import webbrowser
import subprocess
import sys

# ========== ФАЙЛ ДЛЯ СОХРАНЕНИЯ НАСТРОЕК ==========
CONFIG_FILE = "route_config.json"


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}
    return {}


def save_config(address, vehicles=2):
    config = {
        "start_address": address,
        "vehicles": vehicles
    }
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def normalize_address(address):
    address = address.strip()
    if re.search(r'улиц[а-я]|ул\.', address, re.IGNORECASE):
        return address
    parts = [p.strip() for p in address.split(',')]
    if len(parts) == 3:
        city, street, house = parts
        return f"{city}, улица {street}, {house}"
    elif len(parts) == 2:
        city, street = parts
        return f"{city}, улица {street}"
    words = address.split()
    if len(words) >= 3:
        house = words[-1]
        if house.isdigit() or (house.replace('/', '').isdigit()):
            street = " ".join(words[1:-1])
            city = words[0]
            return f"{city}, улица {street}, {house}"
        else:
            city = words[0]
            street = " ".join(words[1:])
            return f"{city}, улица {street}"
    elif len(words) == 2:
        city, street = words
        return f"{city}, улица {street}"
    return address


# ========== ОСНОВНАЯ ЛОГИКА ==========
class RouteOptimizerApp:
    def __init__(self):
        self.config = load_config()
        self.running = False
        self.last_filenames = []

    def geocode_address(self, address):
        geolocator = Nominatim(user_agent="route_optimizer")
        try:
            location = geolocator.geocode(address, timeout=10)
            if location:
                return [location.latitude, location.longitude]
            return None
        except:
            return None

    def get_distance_matrix(self, coords):
        loc_str = ";".join([f"{lon},{lat}" for lat, lon in coords])
        url = f"http://router.project-osrm.org/table/v1/driving/{loc_str}"
        params = {"annotations": "duration,distance"}

        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code != 200:
                n = len(coords)
                durations = [[0] * n for _ in range(n)]
                for i in range(n):
                    for j in range(n):
                        if i != j:
                            durations[i][j] = geodesic(coords[i], coords[j]).meters / 10
                return durations, None
            data = response.json()
            return data["durations"], data["distances"]
        except:
            n = len(coords)
            durations = [[0] * n for _ in range(n)]
            for i in range(n):
                for j in range(n):
                    if i != j:
                        durations[i][j] = geodesic(coords[i], coords[j]).meters / 10
            return durations, None

    def optimize_multi_route(self, durations, num_vehicles=2, start_index=0):
        n = len(durations)

        if num_vehicles > n - 1:
            num_vehicles = n - 1
        if num_vehicles < 1:
            num_vehicles = 1

        all_routes = []
        total_time = 0
        max_route_time = 0

        points = list(range(1, n))
        points_per_vehicle = len(points) // num_vehicles
        remainder = len(points) % num_vehicles

        idx = 0
        for vehicle_id in range(num_vehicles):
            count = points_per_vehicle + (1 if vehicle_id < remainder else 0)
            if count == 0:
                continue

            route = [start_index] + points[idx:idx + count] + [start_index]
            idx += count

            route_time = 0
            for i in range(len(route) - 1):
                route_time += durations[route[i]][route[i + 1]]

            all_routes.append(route)
            total_time += route_time
            if route_time > max_route_time:
                max_route_time = route_time

        return all_routes, total_time, max_route_time

    def create_multi_map(self, route_coords, route_addresses, vehicle_id):
        map_center = route_coords[0]
        m = folium.Map(location=map_center, zoom_start=13)

        colors = ['red', 'blue', 'green', 'purple', 'orange', 'darkred', 'darkblue', 'darkgreen']
        color = colors[(vehicle_id - 1) % len(colors)]

        for i, (lat, lon) in enumerate(route_coords):
            popup_text = route_addresses[i] if i < len(route_addresses) else f"Точка {i + 1}"
            if i == 0 or i == len(route_coords) - 1:
                icon = folium.Icon(color=color, icon="home", prefix='fa')
            else:
                icon = folium.Icon(color=color, icon="info-sign")

            folium.Marker(
                location=[lat, lon],
                popup=f"{i + 1}. {popup_text}",
                icon=icon
            ).add_to(m)

        path = [(lat, lon) for lat, lon in route_coords]
        folium.PolyLine(path, color=color, weight=5, opacity=0.8).add_to(m)

        filename = f"route_vehicle_{vehicle_id}.html"
        m.save(filename)
        return filename

    def optimize(self, start_address, addresses, num_vehicles, update_status, add_output, update_map_buttons):
        try:
            all_addresses = [start_address] + addresses

            add_output(f"🚚 КУРЬЕРОВ: {num_vehicles} | ЗАКАЗОВ: {len(addresses)}\n")
            add_output("=" * 70 + "\n\n")

            # ===== ГЕОКОДИНГ =====
            add_output("🔍 Преобразование адресов в координаты...\n")

            coords = []
            for i, addr in enumerate(all_addresses):
                add_output(f"  Обработка {i + 1}. {addr}\n")

                lat_lon = self.geocode_address(addr)
                if lat_lon:
                    coords.append(lat_lon)
                    add_output(f"    ✅ {lat_lon[0]:.5f}, {lat_lon[1]:.5f}\n")
                else:
                    add_output(f"    ❌ Не удалось найти координаты\n")

            if len(coords) < 2:
                add_output("❌ Не удалось найти координаты для адресов!\n")
                update_status("Ошибка геокодинга")
                return

            # ===== РАСЧЁТ МАТРИЦЫ =====
            add_output("\n🧮 Расчёт матрицы расстояний...\n")
            durations, distances = self.get_distance_matrix(coords)

            # ===== ОПТИМИЗАЦИЯ =====
            add_output(f"\n🔄 Распределение заказов между {num_vehicles} курьерами...\n")

            routes, total_time, max_route_time = self.optimize_multi_route(
                durations,
                num_vehicles=num_vehicles,
                start_index=0
            )

            if routes:
                add_output("\n" + "=" * 70 + "\n")
                add_output("✅ ОПТИМАЛЬНЫЕ МАРШРУТЫ ДЛЯ КУРЬЕРОВ:\n")
                add_output("=" * 70 + "\n")

                total_all_time = 0
                max_time = 0
                max_time_route = 0
                self.last_filenames = []

                for vehicle_idx, route_indices in enumerate(routes):
                    if len(route_indices) <= 1:
                        continue

                    route_addresses = [all_addresses[idx] for idx in route_indices]
                    route_coords = [coords[idx] for idx in route_indices]

                    route_time = 0
                    route_distance = 0

                    for i in range(len(route_indices) - 1):
                        current = route_indices[i]
                        next_point = route_indices[i + 1]
                        route_time += durations[current][next_point]
                        if distances:
                            route_distance += distances[current][next_point]
                        else:
                            route_distance += durations[current][next_point] * 10

                    total_all_time += route_time
                    if route_time > max_time:
                        max_time = route_time
                        max_time_route = vehicle_idx + 1

                    minutes = int(route_time // 60)
                    seconds = int(route_time % 60)
                    km = route_distance / 1000

                    add_output(f"\n🚚 КУРЬЕР {vehicle_idx + 1}:\n")
                    add_output(f"   ⏱️  Время: {minutes} мин {seconds} сек | 📏 {km:.1f} км\n")

                    for pos, addr in enumerate(route_addresses):
                        if pos == 0:
                            add_output(f"   🏠 {addr} (старт)\n")
                        elif pos == len(route_addresses) - 1:
                            add_output(f"   🏠 {addr} (финиш)\n")
                        else:
                            add_output(f"   📍 {pos}. {addr}\n")

                    filename = self.create_multi_map(route_coords, route_addresses, vehicle_idx + 1)
                    self.last_filenames.append(filename)

                add_output("\n" + "=" * 70 + "\n")
                add_output("📊 ОБЩАЯ СТАТИСТИКА:\n")
                add_output("=" * 70 + "\n")

                total_minutes = int(total_all_time // 60)
                total_seconds = int(total_all_time % 60)
                max_minutes = int(max_time // 60)
                max_seconds = int(max_time % 60)

                add_output(f"   ⏱️  Общее время всех курьеров: {total_minutes} мин {total_seconds} сек\n")
                add_output(
                    f"   ⏱️  Самый долгий маршрут (Курьер {max_time_route}): {max_minutes} мин {max_seconds} сек\n")
                add_output(f"   📦 Всего заказов: {len(addresses)}\n")
                add_output(f"   🚚 Всего курьеров: {num_vehicles}\n")
                add_output("=" * 70 + "\n")

                add_output("\n🗺️ Карты сохранены:\n")
                for i, fname in enumerate(self.last_filenames):
                    add_output(f"   ✅ Маршрут курьера {i + 1}: {fname}\n")

                add_output("\n📂 Нажмите кнопку 'Открыть карту' ниже, чтобы посмотреть маршрут на карте\n")

                update_status("✅ Маршруты построены!")
                update_map_buttons(self.last_filenames)

        except Exception as e:
            add_output(f"\n❌ ОШИБКА: {e}\n")
            update_status("❌ Ошибка")


# ========== FLET GUI ==========
def main(page: ft.Page):
    page.title = "🚚 Оптимизатор маршрутов для курьеров"
    page.scroll = ft.ScrollMode.AUTO
    page.theme_mode = ft.ThemeMode.LIGHT
    page.window_width = 400
    page.window_height = 800
    page.window_resizable = True

    config = load_config()
    addresses_list = []
    app = RouteOptimizerApp()
    map_buttons_container = ft.Column(spacing=5)

    # ===== ФУНКЦИЯ ДЛЯ ОТКРЫТИЯ КАРТЫ (ИСПРАВЛЕННАЯ) =====
    def open_map(filename):
        """Открывает HTML-файл в браузере принудительно"""
        try:
            # Получаем абсолютный путь
            file_path = os.path.abspath(filename)
            if not os.path.exists(file_path):
                page.snack_bar = ft.SnackBar(ft.Text(f"❌ Файл не найден: {filename}"))
                page.snack_bar.open = True
                page.update()
                return

            # Преобразуем путь в URL для браузера
            file_url = f"file://{file_path.replace('\\', '/')}"

            # Пытаемся открыть через системный вызов
            try:
                if sys.platform == 'win32':
                    os.startfile(file_path)
                elif sys.platform == 'darwin':  # macOS
                    subprocess.run(['open', file_path])
                else:  # Linux, Android
                    # Пробуем разные браузеры
                    browsers = ['xdg-open', 'google-chrome', 'chromium', 'firefox']
                    for browser in browsers:
                        try:
                            subprocess.run([browser, file_path], check=True)
                            break
                        except:
                            continue
                    else:
                        # Если ничего не работает — используем webbrowser
                        webbrowser.open(file_url)
            except:
                # Запасной вариант
                webbrowser.open(file_url)

            page.snack_bar = ft.SnackBar(ft.Text(f"🗺️ Карта открывается в браузере"))
            page.snack_bar.open = True
            page.update()

        except Exception as e:
            page.snack_bar = ft.SnackBar(ft.Text(f"❌ Ошибка: {e}"))
            page.snack_bar.open = True
            page.update()

    def update_map_buttons(filenames):
        map_buttons_container.controls.clear()
        if filenames:
            map_buttons_container.controls.append(
                ft.Text("🗺️ Открыть карты:", weight=ft.FontWeight.BOLD)
            )
            for i, fname in enumerate(filenames):
                map_buttons_container.controls.append(
                    ft.ElevatedButton(
                        f"📍 Маршрут курьера {i + 1}",
                        on_click=lambda e, f=fname: open_map(f),
                        width=page.window_width - 60,
                        height=40
                    )
                )
        page.update()

    start_address = ft.TextField(
        label="🏠 Стартовый адрес",
        hint_text="Например: Москва, Тверская, 1",
        value=config.get("start_address", ""),
        expand=True
    )

    vehicles_count = ft.TextField(
        label="🚚 Количество курьеров",
        hint_text="2",
        value=str(config.get("vehicles", 2)),
        width=150
    )

    point_address = ft.TextField(
        label="📍 Адрес заказа",
        hint_text="Например: Москва, Арбат, 20",
        expand=True
    )

    orders_list = ft.Column(spacing=5, scroll=ft.ScrollMode.AUTO, height=200)

    def update_orders_display():
        orders_list.controls.clear()
        if not addresses_list:
            orders_list.controls.append(
                ft.Text("Нет заказов", italic=True)
            )
        else:
            for i, addr in enumerate(addresses_list):
                orders_list.controls.append(
                    ft.Row([
                        ft.Text(f"{i + 1}. {addr}", expand=True),
                        ft.ElevatedButton(
                            "✕ Удалить",
                            on_click=lambda e, idx=i: delete_order(idx),
                            height=30,
                            bgcolor="red",
                            color="white"
                        )
                    ])
                )
        page.update()

    def delete_order(idx):
        if 0 <= idx < len(addresses_list):
            addresses_list.pop(idx)
            update_orders_display()

    def add_order(e):
        addr = point_address.value.strip()
        if not addr:
            page.snack_bar = ft.SnackBar(ft.Text("Введите адрес заказа!"))
            page.snack_bar.open = True
            page.update()
            return

        normalized = normalize_address(addr)
        addresses_list.append(normalized)
        point_address.value = ""
        update_orders_display()
        page.snack_bar = ft.SnackBar(ft.Text(f"✅ Добавлен: {addr}"))
        page.snack_bar.open = True
        page.update()

    output_text = ft.TextField(
        label="📊 Результаты",
        multiline=True,
        min_lines=8,
        max_lines=15,
        read_only=True,
        expand=True
    )

    status_text = ft.Text("✅ Готов к работе")

    def add_output(text):
        current = output_text.value or ""
        output_text.value = current + text
        page.update()

    def update_status(text):
        status_text.value = text
        page.update()

    def clear_all_orders(e):
        addresses_list.clear()
        update_orders_display()
        page.snack_bar = ft.SnackBar(ft.Text("🗑️ Все заказы очищены"))
        page.snack_bar.open = True
        page.update()

    def load_from_file(e):
        page.snack_bar = ft.SnackBar(ft.Text("Загрузка из файла будет добавлена позже"))
        page.snack_bar.open = True
        page.update()

    def run_optimization(e):
        start = start_address.value.strip()
        if not start:
            page.snack_bar = ft.SnackBar(ft.Text("Введите стартовый адрес!"))
            page.snack_bar.open = True
            page.update()
            return

        try:
            num_vehicles = int(vehicles_count.value)
            if num_vehicles < 1:
                num_vehicles = 1
        except:
            num_vehicles = 2

        if len(addresses_list) < 1:
            page.snack_bar = ft.SnackBar(ft.Text("Добавьте хотя бы один заказ!"))
            page.snack_bar.open = True
            page.update()
            return

        if num_vehicles > len(addresses_list):
            page.snack_bar = ft.SnackBar(
                ft.Text(f"Курьеров ({num_vehicles}) больше чем заказов ({len(addresses_list)})!")
            )
            page.snack_bar.open = True
            page.update()
            return

        start_normalized = normalize_address(start)
        save_config(start_normalized, num_vehicles)

        output_text.value = ""
        map_buttons_container.controls.clear()
        status_text.value = "⏳ Идёт расчёт..."
        page.update()

        thread = threading.Thread(
            target=app.optimize,
            args=(start_normalized, addresses_list.copy(), num_vehicles, update_status, add_output, update_map_buttons)
        )
        thread.daemon = True
        thread.start()

    # ===== СОЗДАНИЕ ИНТЕРФЕЙСА =====
    page.add(
        ft.Container(
            content=ft.Column([
                ft.Card(
                    content=ft.Container(
                        content=ft.Text(
                            "ℹ️ Введите адреса в любом формате: 'Москва, Тверская, 1'",
                            size=12
                        ),
                        padding=10
                    )
                ),
                ft.Card(
                    content=ft.Container(
                        content=ft.Column([
                            ft.Text("🏠 Стартовая точка", weight=ft.FontWeight.BOLD),
                            start_address,
                            ft.Row([
                                ft.Text("Курьеров:", size=14),
                                vehicles_count,
                            ])
                        ]),
                        padding=10
                    )
                ),
                ft.Card(
                    content=ft.Container(
                        content=ft.Column([
                            ft.Text("📍 Заказы", weight=ft.FontWeight.BOLD),
                            ft.Row([
                                point_address,
                                ft.ElevatedButton(
                                    "➕ Добавить",
                                    on_click=add_order,
                                    height=40
                                )
                            ]),
                            ft.Text("Список заказов:", size=12),
                            orders_list,
                            ft.Row([
                                ft.ElevatedButton(
                                    "🗑️ Очистить все",
                                    on_click=clear_all_orders,
                                    height=40
                                ),
                                ft.ElevatedButton(
                                    "📂 Загрузить",
                                    on_click=load_from_file,
                                    height=40
                                )
                            ])
                        ]),
                        padding=10
                    )
                ),
                ft.ElevatedButton(
                    "🚀 ПОСТРОИТЬ ОПТИМАЛЬНЫЕ МАРШРУТЫ",
                    on_click=run_optimization,
                    width=page.window_width - 40,
                    height=50
                ),
                ft.Card(
                    content=ft.Container(
                        content=ft.Column([
                            ft.Text("📊 Результаты", weight=ft.FontWeight.BOLD),
                            output_text,
                            status_text,
                            ft.Divider(height=10),
                            map_buttons_container
                        ]),
                        padding=10
                    )
                )
            ]),
            padding=20
        )
    )

    update_orders_display()


if __name__ == "__main__":
    ft.run(main, view=ft.AppView.WEB_BROWSER)