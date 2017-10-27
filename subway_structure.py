import csv
import logging
import math
import urllib.parse
import urllib.request
from collections import Counter, defaultdict


SPREADSHEET_ID = '1-UHDzfBwHdeyFxgC5cE_MaNQotF3-Y0r1nW9IwpIEj8'
MODES = ('subway', 'light_rail', 'monorail')
MAX_DISTANCE_NEARBY = 150  # in meters
ALLOWED_STATIONS_MISMATCH = 0.02   # part of total station count
ALLOWED_TRANSFERS_MISMATCH = 0.07  # part of total interchanges count
CONSTRUCTION_KEYS = ('construction', 'proposed', 'construction:railway', 'proposed:railway')

transfers = []
used_entrances = set()


def el_id(el):
    if not el:
        return None
    if 'type' not in el:
        raise Exception('What is this element? {}'.format(el))
    return el['type'][0] + str(el.get('id', el.get('ref', '')))


def el_center(el):
    if 'lat' in el:
        return (el['lon'], el['lat'])
    elif 'center' in el:
        if el['center']['lat'] == 0.0:
            # Some relations don't have centers. We need route_masters and stop_area_groups.
            if el['type'] == 'relation' and 'tags' in el and (
                    el['tags'].get('type') == 'route_master' or
                    el['tags'].get('public_transport') == 'stop_area_group'):
                return None
        return (el['center']['lon'], el['center']['lat'])
    return None


def distance(p1, p2):
    if p1 is None or p2 is None:
        return None
    dx = math.radians(p1[0] - p2[0]) * math.cos(
        0.5 * math.radians(p1[1] + p2[1]))
    dy = math.radians(p1[1] - p2[1])
    return 6378137 * math.sqrt(dx*dx + dy*dy)


def format_elid_list(ids):
    msg = ', '.join(list(ids)[:20])
    if len(ids) > 20:
        msg += ', ...'
    return msg


class Station:
    @staticmethod
    def get_modes(el):
        mode = el['tags'].get('station')
        modes = [] if not mode else [mode]
        for m in MODES:
            if el['tags'].get(m) == 'yes':
                modes.append(m)
        return set(modes)

    @staticmethod
    def is_station(el):
        if el.get('tags', {}).get('railway') not in ('station', 'halt'):
            return False
        for k in CONSTRUCTION_KEYS:
            if k in el['tags']:
                return False
        if Station.get_modes(el).isdisjoint(MODES):
            return False
        return True

    def __init__(self, el, city):
        """Call this with a railway=station node."""
        if el.get('tags', {}).get('railway') not in ('station', 'halt'):
            raise Exception(
                'Station object should be instantiated from a station node. Got: {}'.format(el))
        if not Station.is_station(el):
            raise Exception('Processing only subway and light rail stations')

        if el['type'] != 'node':
            city.warn('Station is not a node', el)

        self.id = el_id(el)
        self.element = el
        self.modes = Station.get_modes(el)
        self.name = el['tags'].get('name', '?')
        self.int_name = el['tags'].get('int_name', el['tags'].get('name:en', None))
        self.colour = el['tags'].get('colour', None)
        self.center = el_center(el)
        if self.center is None:
            raise Exception('Could not find center of {}'.format(el))


class StopArea:
    @staticmethod
    def is_stop_or_platform(el):
        if 'tags' not in el:
            return False
        if el['tags'].get('railway') == 'platform':
            return True
        if el['tags'].get('public_transport') in ('platform', 'stop_position'):
            return True
        return False

    def __init__(self, station, city, stop_area=None):
        """Call this with a Station object."""

        self.id = el_id(stop_area) if stop_area else station.id
        self.stop_area = stop_area
        self.station = station
        self.stops_and_platforms = set()  # set of el_ids of platforms and stop_positions
        self.exits = set()  # el_id of subway_entrance for leaving the platform
        self.entrances = set()  # el_id of subway_entrance for entering the platform
        self.center = None  # lon, lat of the station centre point
        self.centers = {}  # el_id -> (lon, lat) for all elements

        self.modes = station.modes
        self.name = station.name
        self.int_name = station.int_name
        self.colour = station.colour

        if stop_area:
            self.name = stop_area['tags'].get('name', self.name)
            self.int_name = stop_area['tags'].get(
                'int_name', stop_area['tags'].get('name:en', self.int_name))
            self.colour = stop_area['tags'].get('colour', self.colour)

            # If we have a stop area, add all elements from it
            warned_about_tracks = False
            for m in stop_area['members']:
                k = el_id(m)
                m_el = city.elements.get(k)
                if m_el and 'tags' in m_el:
                    if Station.is_station(m_el):
                        if k != station.id:
                            city.error('Stop area has multiple stations', stop_area)
                    elif StopArea.is_stop_or_platform(m_el):
                        self.stops_and_platforms.add(k)
                    elif m_el['tags'].get('railway') == 'subway_entrance':
                        if m_el['type'] != 'node':
                            city.warn('Subway entrance is not a node', m_el)
                        if m_el['tags'].get('entrance') != 'exit' and m['role'] != 'exit_only':
                            self.entrances.add(k)
                        if m_el['tags'].get('entrance') != 'entrance' and m['role'] != 'entry_only':
                            self.exits.add(k)
                    elif m_el['tags'].get('railway') in ['rail'] + list(MODES):
                        if not warned_about_tracks:
                            city.error('Tracks in a stop_area relation', stop_area)
                            warned_about_tracks = True
        else:
            # Otherwise add nearby entrances and stop positions
            center = station.center
            for c_el in city.elements.values():
                c_id = el_id(c_el)
                c_center = el_center(c_el)
                if 'tags' not in c_el or not c_center:
                    continue
                if StopArea.is_stop_or_platform(c_el):
                    # Take care to not add other stations
                    if 'station' not in c_el['tags']:
                        if distance(center, c_center) <= MAX_DISTANCE_NEARBY:
                            self.stops_and_platforms.add(c_id)
                elif c_el['tags'].get('railway') == 'subway_entrance':
                    if distance(center, c_center) <= MAX_DISTANCE_NEARBY:
                        if c_el['type'] != 'node':
                            city.warn('Subway entrance is not a node', c_el)
                        etag = c_el['tags'].get('entrance')
                        if etag != 'exit':
                            self.entrances.add(c_id)
                        if etag != 'entrance':
                            self.exits.add(c_id)

        if self.exits and not self.entrances:
            city.error('Only exits for a station, no entrances', stop_area or station.element)
        if self.entrances and not self.exits:
            city.error('No exits for a station', stop_area or station.element)

        """Calculates the center point of the station. This algorithm
        cannot rely on a station node, since many stop_areas can share one.
        Basically it averages center points of all platforms
        and stop positions."""
        if len(self.stops_and_platforms) == 0:
            self.center = station.center
        else:
            self.center = [0, 0]
            for sp in self.stops_and_platforms:
                spc = el_center(city.elements[sp])
                if spc:
                    for i in range(2):
                        self.center[i] += spc[i]
            for i in range(2):
                self.center[i] /= len(self.stops_and_platforms)

        for el in self.get_elements():
            self.centers[el] = el_center(city.elements[el])

    def get_elements(self):
        result = set([self.id, self.station.id])
        result.update(self.entrances)
        result.update(self.exits)
        result.update(self.stops_and_platforms)
        return result


class Route:
    """The longest route for a city with a unique ref."""
    @staticmethod
    def is_route(el):
        if el['type'] != 'relation' or el.get('tags', {}).get('type') != 'route':
            return False
        if 'members' not in el:
            return False
        if el['tags'].get('route') not in MODES:
            return False
        for k in CONSTRUCTION_KEYS:
            if k in el['tags']:
                return False
        if 'ref' not in el['tags'] and 'name' not in el['tags']:
            return False
        return True

    @staticmethod
    def get_network(relation):
        return relation['tags'].get('network', relation['tags'].get('operator', None))

    def __init__(self, relation, city):
        if not Route.is_route(relation):
            raise Exception('The relation does not seem a route: {}'.format(relation))
        self.element = relation
        self.id = el_id(relation)
        if 'ref' not in relation['tags']:
            city.warn('Missing ref on a route', relation)
        self.ref = relation['tags'].get('ref', relation['tags'].get('name', None))
        self.name = relation['tags'].get('name', None)
        if 'colour' not in relation['tags']:
            city.warn('Missing colour on a route', relation)
        self.colour = relation['tags'].get('colour', None)
        self.network = Route.get_network(relation)
        self.mode = relation['tags']['route']
        self.rails = []
        self.stops = []
        # Add circular=yes on a route to disable station order checking
        # This is a hack, but few lines actually have repeating stops
        is_circle = relation['tags'].get('circular') == 'yes'
        enough_stops = False
        for m in relation['members']:
            k = el_id(m)
            if k in city.stations:
                st_list = city.stations[k]
                st = st_list[0]
                if len(st_list) > 1:
                    city.error('Ambigous station {} in route. Please use stop_position or split '
                               'interchange stations'.format(st.name), relation)
                if not self.stops or self.stops[-1] != st:
                    if enough_stops:
                        if st not in self.stops:
                            city.error('Inconsistent platform-stop "{}" in route'.format(st.name),
                                       relation)
                    elif st not in self.stops or is_circle:
                        self.stops.append(st)
                        if self.mode not in st.modes:
                            city.warn('{} station "{}" in {} route'.format(
                                '+'.join(st.modes), st.name, self.mode), relation)
                    elif self.stops[0] == st and not enough_stops:
                        enough_stops = True
                    else:
                        city.error(
                            'Duplicate stop "{}" in route - check stop/platform order'.format(
                                st.name), relation)
                continue

            if k not in city.elements:
                if m['role'] in ('stop', 'platform'):
                    city.error('{} {} {} for route relation is not in the dataset'.format(
                        m['role'], m['type'], m['ref']), relation)
                    raise Exception('Stop or platform {} {} in relation {} '
                                    'is not in the dataset'.format(
                                        m['type'], m['ref'], relation['id']))
                continue
            el = city.elements[k]
            if 'tags' not in el:
                city.error('Untagged object in a route', relation)
                continue
            if m['role'] in ('stop', 'platform'):
                for k in CONSTRUCTION_KEYS:
                    if k in el['tags']:
                        city.error('An under construction {} in route'.format(m['role']), el)
                        continue
                if el['tags'].get('railway') in ('station', 'halt'):
                    city.error('Missing station={} on a {}'.format(self.mode, m['role']), el)
                else:
                    city.error('{} {} {} is not connected to a station in route'.format(
                        m['role'], m['type'], m['ref']), relation)
            if el['tags'].get('railway') in ('rail', 'subway', 'light_rail', 'monorail'):
                if 'nodes' in el:
                    self.rails.append((el['nodes'][0], el['nodes'][-1]))
                else:
                    city.error('Cannot find nodes in a railway', el)
                continue
        if not self.stops:
            city.error('Route has no stops', relation)
        for i in range(1, len(self.rails)):
            connected = sum([(1 if self.rails[i][j[0]] == self.rails[i-1][j[1]] else 0)
                             for j in ((0, 0), (0, 1), (1, 0), (1, 1))])
            if not connected:
                city.warn('Hole in route rails near node {}'.format(self.rails[i][0]), relation)
                break

    def __len__(self):
        return len(self.stops)

    def __get__(self, i):
        return self.stops[i]

    def __iter__(self):
        return iter(self.stops)


class RouteMaster:
    def __init__(self, master=None):
        self.routes = []
        self.best = None
        self.id = el_id(master)
        self.has_master = master is not None
        if master:
            self.ref = master['tags'].get('ref', master['tags'].get('name', None))
            self.colour = master['tags'].get('colour', None)
            self.network = Route.get_network(master)
            self.mode = master['tags'].get('route_master', None)  # This tag is required, but okay
            self.name = master['tags'].get('name', None)
        else:
            self.ref = None
            self.colour = None
            self.network = None
            self.mode = None
            self.name = None

    def add(self, route, city):
        if not self.network:
            self.network = route.network
        elif route.network and route.network != self.network:
            city.error('Route has different network ("{}") from master "{}"'.format(
                route.network, self.network), route.element)

        if not self.colour:
            self.colour = route.colour
        elif route.colour and route.colour != self.colour:
            city.warn('Route "{}" has different colour from master "{}"'.format(
                route.colour, self.colour), route.element)

        if not self.ref:
            self.ref = route.ref
        elif route.ref != self.ref:
            city.warn('Route "{}" has different ref from master "{}"'.format(
                route.ref, self.ref), route.element)

        if not self.name:
            self.name = route.name

        if not self.mode:
            self.mode = route.mode
        elif route.mode != self.mode:
            city.error('Incompatible PT mode: master has {} and route has {}'.format(
                self.mode, route.mode), route.element)
            return

        if not self.has_master and (not self.id or self.id > route.id):
            self.id = route.id

        self.routes.append(route)
        if not self.best or len(route.stops) > len(self.best.stops):
            self.best = route

    def __len__(self):
        return len(self.routes)

    def __get__(self, i):
        return self.routes[i]

    def __iter__(self):
        return iter(self.routes)


class City:
    def __init__(self, row):
        self.name = row[0]
        self.country = row[1]
        self.continent = row[2]
        self.num_stations = int(row[3])
        self.num_lines = int(row[4] or '0')
        self.num_light_lines = int(row[5] or '0')
        self.num_interchanges = int(row[6] or '0')
        self.networks = set(filter(None, [x.strip() for x in row[8].split(';')]))
        bbox = row[7].split(',')
        if len(bbox) == 4:
            self.bbox = [float(bbox[i]) for i in (1, 0, 3, 2)]
        else:
            self.bbox = None
        self.elements = {}   # Dict el_id → el
        self.stations = defaultdict(list)   # Dict el_id → list of stop areas
        self.routes = {}     # Dict route_ref → route
        self.masters = {}    # Dict el_id of route → route_master
        self.stop_areas = defaultdict(list)  # El_id → list of el_id of stop_area
        self.transfers = []  # List of lists of stop areas
        self.station_ids = set()  # Set of stations' uid
        self.stops_and_platforms = set()  # Set of stops and platforms el_id
        self.errors = []
        self.warnings = []

    def contains(self, el):
        center = el_center(el)
        if center:
            return (self.bbox[0] <= center[1] <= self.bbox[2] and
                    self.bbox[1] <= center[0] <= self.bbox[3])
        if 'tags' not in el:
            return False
        return 'route_master' in el['tags'] or 'public_transport' in el['tags']

    def add(self, el):
        if el['type'] == 'relation' and 'members' not in el:
            return
        self.elements[el_id(el)] = el
        if el['type'] == 'relation' and 'tags' in el:
            if el['tags'].get('type') == 'route_master':
                for m in el['members']:
                    if m['type'] == 'relation':
                        if el_id(m) in self.masters:
                            self.error('Route in two route_masters', m)
                        self.masters[el_id(m)] = el
            elif el['tags'].get('public_transport') == 'stop_area':
                warned_about_duplicates = False
                for m in el['members']:
                    stop_area = self.stop_areas[el_id(m)]
                    if el in stop_area:
                        if not warned_about_duplicates:
                            self.warn('Duplicate element in a stop area', el)
                            warned_about_duplicates = True
                    else:
                        stop_area.append(el)

    def get_validation_result(self):
        result = {
            'name': self.name,
            'country': self.country,
            'continent': self.continent,
            'stations_expected': self.num_stations,
            'subwayl_expected': self.num_lines,
            'lightrl_expected': self.num_light_lines,
            'transfers_expected': self.num_interchanges,
            'stations_found': self.found_stations,
            'subwayl_found': self.found_lines,
            'lightrl_found': self.found_light_lines,
            'transfers_found': self.found_interchanges,
            'unused_entrances': self.unused_entrances,
            'networks': self.found_networks,
        }
        result['warnings'] = self.warnings
        result['errors'] = self.errors
        return result

    def log_message(self, message, el):
        if el:
            tags = el.get('tags', {})
            message += ' ({} {}, "{}")'.format(
                el['type'], el.get('id', el.get('ref')),
                tags.get('name', tags.get('ref', '')))
        return message

    def warn(self, message, el=None):
        msg = self.log_message(message, el)
        self.warnings.append(msg)

    def error(self, message, el=None):
        msg = self.log_message(message, el)
        self.errors.append(msg)

    def make_transfer(self, sag):
        transfer = set()
        for m in sag['members']:
            k = el_id(m)
            if k in self.stations:
                transfer.add(self.stations[k][0])
        if len(transfer) > 1:
            self.transfers.append(transfer)

    def is_good(self):
        return len(self.errors) == 0

    def extract_routes(self):
        # Extract stations
        processed_stop_areas = set()
        for el in self.elements.values():
            if Station.is_station(el):
                st = Station(el, self)
                self.station_ids.add(st.id)
                if st.id in self.stop_areas:
                    stations = []
                    for sa in self.stop_areas[st.id]:
                        stations.append(StopArea(st, self, sa))
                else:
                    stations = [StopArea(st, self)]

                for station in stations:
                    if station.id not in processed_stop_areas:
                        processed_stop_areas.add(station.id)
                        for st_el in station.get_elements():
                            self.stations[st_el].append(station)

                        # Check that stops and platforms belong to single stop_area
                        for sp in station.stops_and_platforms:
                            if sp in self.stops_and_platforms:
                                self.warn('A stop or a platform {} belongs to multiple '
                                          'stations, might be correct'.format(sp))
                            else:
                                self.stops_and_platforms.add(sp)

        # Extract routes
        for el in self.elements.values():
            if Route.is_route(el):
                route_id = el_id(el)
                if self.networks:
                    network = Route.get_network(el)
                    if route_id in self.masters:
                        master_network = Route.get_network(self.masters[route_id])
                    else:
                        master_network = None
                    if network not in self.networks and master_network not in self.networks:
                        continue

                route = Route(el, self)
                if route.id in self.masters:
                    master = self.masters[route.id]
                    k = el_id(master)
                else:
                    master = None
                    k = route.ref
                if k not in self.routes:
                    self.routes[k] = RouteMaster(master)
                self.routes[k].add(route, self)

                # Sometimes adding a route to a newly initialized RouteMaster can fail
                if len(self.routes[k]) == 0:
                    del self.routes[k]

            # And while we're iterating over relations, find interchanges
            if (el['type'] == 'relation' and
                    el.get('tags', {}).get('public_transport', None) == 'stop_area_group'):
                self.make_transfer(el)

        # Filter transfers, leaving only stations that belong to routes
        used_stop_areas = set()
        for rmaster in self.routes.values():
            for route in rmaster:
                used_stop_areas.update(route.stops)
        new_transfers = []
        for transfer in self.transfers:
            new_tr = [s for s in transfer if s in used_stop_areas]
            if len(new_tr) > 1:
                new_transfers.append(new_tr)
        self.transfers = new_transfers

    def __iter__(self):
        return iter(self.routes.values())

    def count_unused_entrances(self):
        global used_entrances
        stop_areas = set()
        for el in self.elements.values():
            if (el['type'] == 'relation' and 'tags' in el and
                    el['tags'].get('public_transport') == 'stop_area' and
                    'members' in el):
                stop_areas.update([el_id(m) for m in el['members']])
        unused = []
        not_in_sa = []
        for el in self.elements.values():
            if (el['type'] == 'node' and 'tags' in el and
                    el['tags'].get('railway') == 'subway_entrance'):
                i = el_id(el)
                if i in self.stations:
                    used_entrances.add(i)
                if i not in stop_areas:
                    not_in_sa.append(i)
                    if i not in self.stations:
                        unused.append(i)
        self.unused_entrances = len(unused)
        self.entrances_not_in_stop_areas = len(not_in_sa)
        if unused:
            self.error('Found {} entrances not used in routes or stop_areas: {}'.format(
                len(unused), format_elid_list(unused)))
        if not_in_sa:
            self.warn('{} subway entrances are not in stop_area relations'.format(len(not_in_sa)))

    def validate(self):
        networks = Counter()
        unused_stations = set(self.station_ids)
        for rmaster in self.routes.values():
            networks[str(rmaster.network)] += 1
            for route in rmaster:
                for st in route.stops:
                    unused_stations.discard(st.station.id)
        if unused_stations:
            self.unused_stations = len(unused_stations)
            self.warn('{} unused stations: {}'.format(
                self.unused_stations, format_elid_list(unused_stations)))
        self.count_unused_entrances()

        self.found_light_lines = len([x for x in self.routes.values() if x.mode != 'subway'])
        self.found_lines = len(self.routes) - self.found_light_lines
        if self.found_lines != self.num_lines:
            self.error('Found {} subway lines, expected {}'.format(
                self.found_lines, self.num_lines))
        if self.found_light_lines != self.num_light_lines:
            self.error('Found {} light rail lines, expected {}'.format(
                self.found_light_lines, self.num_light_lines))

        self.found_stations = len(self.station_ids) - len(unused_stations)
        if self.found_stations != self.num_stations:
            msg = 'Found {} stations in routes, expected {}'.format(
                self.found_stations, self.num_stations)
            if (0 <= (self.num_stations - self.found_stations) / self.num_stations <=
                    ALLOWED_STATIONS_MISMATCH):
                self.warn(msg)
            else:
                self.error(msg)

        self.found_interchanges = len(self.transfers)
        if self.found_interchanges != self.num_interchanges:
            msg = 'Found {} interchanges, expected {}'.format(
                self.found_interchanges, self.num_interchanges)
            if (self.num_interchanges == 0 or
                    (0 <= (self.num_interchanges - self.found_interchanges) /
                     self.num_interchanges <= ALLOWED_TRANSFERS_MISMATCH)):
                self.warn(msg)
            else:
                self.error(msg)

        self.found_networks = len(networks)
        if len(networks) > max(1, len(self.networks)):
            n_str = '; '.join(['{} ({})'.format(k, v) for k, v in networks.items()])
            self.warn('More than one network: {}'.format(n_str))


def find_transfers(elements, cities):
    global transfers
    transfers = []
    stop_area_groups = []
    for el in elements:
        if (el['type'] == 'relation' and 'members' in el and
                el.get('tags', {}).get('public_transport') == 'stop_area_group'):
            stop_area_groups.append(el)

    stations = defaultdict(set)  # el_id -> list of station objects
    for city in cities:
        for el, st in city.stations.items():
            stations[el].update(st)

    for sag in stop_area_groups:
        transfer = set()
        for m in sag['members']:
            k = el_id(m)
            if k not in stations:
                continue
            transfer.update(stations[k])
        if len(transfer) > 1:
            transfers.append(transfer)
    return transfers


def get_unused_entrances_geojson(elements):
    global used_entrances
    features = []
    for el in elements:
        if (el['type'] == 'node' and 'tags' in el and
                el['tags'].get('railway') == 'subway_entrance'):
            if el_id(el) not in used_entrances:
                geometry = {'type': 'Point', 'coordinates': el_center(el)}
                properties = {k: v for k, v in el['tags'].items()
                              if k not in ('railway', 'entrance')}
                features.append({'type': 'Feature', 'geometry': geometry, 'properties': properties})
    return {'type': 'FeatureCollection', 'features': features}


def download_cities():
    url = 'https://docs.google.com/spreadsheets/d/{}/export?format=csv'.format(SPREADSHEET_ID)
    response = urllib.request.urlopen(url)
    if response.getcode() != 200:
        raise Exception('Failed to download cities spreadsheet: HTTP {}'.format(response.getcode()))
    data = response.read().decode('utf-8')
    r = csv.reader(data.splitlines())
    next(r)  # skipping the header
    names = set()
    cities = []
    for row in r:
        if len(row) > 7 and row[7]:
            cities.append(City(row))
            if row[0].strip() in names:
                logging.warning('Duplicate city name in the google spreadsheet: %s', row[0])
            names.add(row[0].strip())
    return cities
