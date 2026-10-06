"""Geography reference data for query planning: French departments/regions, major cities, countries.

Populations are approximate (latest census order of magnitude) and only used for ordering.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

from scout.util.text import normalize_key


@dataclass(frozen=True)
class Region:
    code: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Department:
    code: str
    name: str
    region_code: str

    @property
    def region(self) -> Region:
        return FR_REGIONS[self.region_code]


@dataclass(frozen=True)
class City:
    name: str                    # local name (what OSM / maps / registries use)
    country: str                 # ISO-3166 alpha-2
    population: int = 0
    department: str | None = None  # FR department code
    region: str | None = None      # region / state name
    aliases: tuple[str, ...] = field(default=())


# ---- France -------------------------------------------------------------------------------

FR_REGIONS: dict[str, Region] = {
    r.code: r
    for r in (
        Region("84", "Auvergne-Rhône-Alpes", ("ARA", "AURA", "Auvergne Rhone Alpes")),
        Region("27", "Bourgogne-Franche-Comté", ("BFC", "Burgundy-Franche-Comte")),
        Region("53", "Bretagne", ("Brittany",)),
        Region("24", "Centre-Val de Loire", ("Centre", "Centre Val de Loire")),
        Region("94", "Corse", ("Corsica",)),
        Region("44", "Grand Est", ("Grand-Est",)),
        Region("32", "Hauts-de-France", ("HDF",)),
        Region("11", "Île-de-France", ("IDF", "Ile de France", "Paris region", "Région parisienne", "Greater Paris")),
        Region("28", "Normandie", ("Normandy",)),
        Region("75", "Nouvelle-Aquitaine", ("Nouvelle Aquitaine",)),
        Region("76", "Occitanie", ()),
        Region("52", "Pays de la Loire", ("Pays-de-la-Loire",)),
        Region("93", "Provence-Alpes-Côte d'Azur", ("PACA", "Région Sud", "Sud", "Provence Alpes Cote d Azur")),
        Region("01", "Guadeloupe", ()),
        Region("02", "Martinique", ()),
        Region("03", "Guyane", ("French Guiana",)),
        Region("04", "La Réunion", ("Reunion", "Réunion")),
        Region("06", "Mayotte", ()),
    )
}

_DEPTS: tuple[tuple[str, str, str], ...] = (
    ("01", "Ain", "84"), ("02", "Aisne", "32"), ("03", "Allier", "84"), ("04", "Alpes-de-Haute-Provence", "93"),
    ("05", "Hautes-Alpes", "93"), ("06", "Alpes-Maritimes", "93"), ("07", "Ardèche", "84"), ("08", "Ardennes", "44"),
    ("09", "Ariège", "76"), ("10", "Aube", "44"), ("11", "Aude", "76"), ("12", "Aveyron", "76"),
    ("13", "Bouches-du-Rhône", "93"), ("14", "Calvados", "28"), ("15", "Cantal", "84"), ("16", "Charente", "75"),
    ("17", "Charente-Maritime", "75"), ("18", "Cher", "24"), ("19", "Corrèze", "75"), ("2A", "Corse-du-Sud", "94"),
    ("2B", "Haute-Corse", "94"), ("21", "Côte-d'Or", "27"), ("22", "Côtes-d'Armor", "53"), ("23", "Creuse", "75"),
    ("24", "Dordogne", "75"), ("25", "Doubs", "27"), ("26", "Drôme", "84"), ("27", "Eure", "28"),
    ("28", "Eure-et-Loir", "24"), ("29", "Finistère", "53"), ("30", "Gard", "76"), ("31", "Haute-Garonne", "76"),
    ("32", "Gers", "76"), ("33", "Gironde", "75"), ("34", "Hérault", "76"), ("35", "Ille-et-Vilaine", "53"),
    ("36", "Indre", "24"), ("37", "Indre-et-Loire", "24"), ("38", "Isère", "84"), ("39", "Jura", "27"),
    ("40", "Landes", "75"), ("41", "Loir-et-Cher", "24"), ("42", "Loire", "84"), ("43", "Haute-Loire", "84"),
    ("44", "Loire-Atlantique", "52"), ("45", "Loiret", "24"), ("46", "Lot", "76"), ("47", "Lot-et-Garonne", "75"),
    ("48", "Lozère", "76"), ("49", "Maine-et-Loire", "52"), ("50", "Manche", "28"), ("51", "Marne", "44"),
    ("52", "Haute-Marne", "44"), ("53", "Mayenne", "52"), ("54", "Meurthe-et-Moselle", "44"), ("55", "Meuse", "44"),
    ("56", "Morbihan", "53"), ("57", "Moselle", "44"), ("58", "Nièvre", "27"), ("59", "Nord", "32"),
    ("60", "Oise", "32"), ("61", "Orne", "28"), ("62", "Pas-de-Calais", "32"), ("63", "Puy-de-Dôme", "84"),
    ("64", "Pyrénées-Atlantiques", "75"), ("65", "Hautes-Pyrénées", "76"), ("66", "Pyrénées-Orientales", "76"),
    ("67", "Bas-Rhin", "44"), ("68", "Haut-Rhin", "44"), ("69", "Rhône", "84"), ("70", "Haute-Saône", "27"),
    ("71", "Saône-et-Loire", "27"), ("72", "Sarthe", "52"), ("73", "Savoie", "84"), ("74", "Haute-Savoie", "84"),
    ("75", "Paris", "11"), ("76", "Seine-Maritime", "28"), ("77", "Seine-et-Marne", "11"), ("78", "Yvelines", "11"),
    ("79", "Deux-Sèvres", "75"), ("80", "Somme", "32"), ("81", "Tarn", "76"), ("82", "Tarn-et-Garonne", "76"),
    ("83", "Var", "93"), ("84", "Vaucluse", "93"), ("85", "Vendée", "52"), ("86", "Vienne", "75"),
    ("87", "Haute-Vienne", "75"), ("88", "Vosges", "44"), ("89", "Yonne", "27"), ("90", "Territoire de Belfort", "27"),
    ("91", "Essonne", "11"), ("92", "Hauts-de-Seine", "11"), ("93", "Seine-Saint-Denis", "11"),
    ("94", "Val-de-Marne", "11"), ("95", "Val-d'Oise", "11"), ("971", "Guadeloupe", "01"), ("972", "Martinique", "02"),
    ("973", "Guyane", "03"), ("974", "La Réunion", "04"), ("976", "Mayotte", "06"),
)

FR_DEPARTMENTS: dict[str, Department] = {c: Department(c, n, r) for c, n, r in _DEPTS}

# Pre-2016 regions and common sub-regional names → departments.
FR_HISTORIC_REGIONS: dict[str, tuple[str, ...]] = {
    "Alsace": ("67", "68"),
    "Lorraine": ("54", "55", "57", "88"),
    "Champagne-Ardenne": ("08", "10", "51", "52"),
    "Rhône-Alpes": ("01", "07", "26", "38", "42", "69", "73", "74"),
    "Auvergne": ("03", "15", "43", "63"),
    "Aquitaine": ("24", "33", "40", "47", "64"),
    "Limousin": ("19", "23", "87"),
    "Poitou-Charentes": ("16", "17", "79", "86"),
    "Languedoc-Roussillon": ("11", "30", "34", "48", "66"),
    "Midi-Pyrénées": ("09", "12", "31", "32", "46", "65", "81", "82"),
    "Nord-Pas-de-Calais": ("59", "62"),
    "Picardie": ("02", "60", "80"),
    "Bourgogne": ("21", "58", "71", "89"),
    "Franche-Comté": ("25", "39", "70", "90"),
    "Haute-Normandie": ("27", "76"),
    "Basse-Normandie": ("14", "50", "61"),
    "Côte d'Azur": ("06",),
    "Provence": ("04", "13", "83", "84"),
    "Savoie Mont Blanc": ("73", "74"),
    "Pays Basque": ("64",),
    "Petite Couronne": ("92", "93", "94"),
    "Grand Paris": ("75", "92", "93", "94"),
}

# Ordered by economic weight (number of active establishments), used to prioritise registry queries.
_DEPT_PRIORITY = (
    "75", "69", "13", "92", "33", "31", "59", "44", "06", "67", "34", "35", "93", "94", "78", "91", "95", "77",
    "38", "83", "76", "62", "57", "74", "54", "63", "37", "45", "30", "64", "29", "56", "84", "42", "49", "17",
    "66", "21", "14", "85", "60", "72", "51", "25", "68", "26", "86", "87", "01", "22", "73", "974", "2A", "2B",
    "971", "972", "40", "11", "27", "28", "24", "81", "71", "47", "50", "16", "41", "79", "02", "80", "10", "18",
    "03", "53", "82", "88", "65", "12", "19", "89", "39", "36", "58", "61", "70", "52", "07", "46", "55", "08",
    "09", "32", "04", "05", "43", "15", "90", "23", "48", "973", "976",
)
DEPARTMENTS_BY_ECONOMIC_SIZE: tuple[str, ...] = tuple(
    dict.fromkeys([*_DEPT_PRIORITY, *(c for c, _, _ in _DEPTS)])
)

_FR_CITIES: tuple[tuple[str, str, int], ...] = (
    ("Paris", "75", 2_133_000), ("Marseille", "13", 873_000), ("Lyon", "69", 522_000), ("Toulouse", "31", 504_000),
    ("Nice", "06", 348_000), ("Nantes", "44", 323_000), ("Montpellier", "34", 302_000), ("Strasbourg", "67", 291_000),
    ("Bordeaux", "33", 262_000), ("Lille", "59", 237_000), ("Rennes", "35", 225_000), ("Toulon", "83", 180_000),
    ("Reims", "51", 178_000), ("Saint-Étienne", "42", 173_000), ("Le Havre", "76", 166_000), ("Dijon", "21", 158_000),
    ("Angers", "49", 157_000), ("Villeurbanne", "69", 157_000), ("Grenoble", "38", 156_000),
    ("Saint-Denis", "974", 154_000), ("Nîmes", "30", 148_000), ("Aix-en-Provence", "13", 147_000),
    ("Clermont-Ferrand", "63", 147_000), ("Le Mans", "72", 145_000), ("Brest", "29", 139_000), ("Tours", "37", 137_000),
    ("Amiens", "80", 134_000), ("Limoges", "87", 131_000), ("Annecy", "74", 131_000), ("Boulogne-Billancourt", "92", 121_000),
    ("Metz", "57", 121_000), ("Perpignan", "66", 119_000), ("Besançon", "25", 119_000), ("Orléans", "45", 116_000),
    ("Rouen", "76", 114_000), ("Saint-Denis", "93", 114_000), ("Montreuil", "93", 111_000), ("Argenteuil", "95", 110_000),
    ("Caen", "14", 108_000), ("Mulhouse", "68", 108_000), ("Saint-Paul", "974", 105_000), ("Nancy", "54", 104_000),
    ("Tourcoing", "59", 99_000), ("Roubaix", "59", 99_000), ("Nanterre", "92", 97_000), ("Vitry-sur-Seine", "94", 95_000),
    ("Créteil", "94", 92_000), ("Avignon", "84", 92_000), ("Poitiers", "86", 90_000), ("Aubervilliers", "93", 89_000),
    ("Asnières-sur-Seine", "92", 87_000), ("Aulnay-sous-Bois", "93", 87_000), ("Colombes", "92", 87_000),
    ("Dunkerque", "59", 86_000), ("Saint-Pierre", "974", 85_000), ("Versailles", "78", 84_000),
    ("Courbevoie", "92", 82_000), ("La Rochelle", "17", 80_000), ("Le Tampon", "974", 80_000), ("Béziers", "34", 79_000),
    ("Cherbourg-en-Cotentin", "50", 79_000), ("Rueil-Malmaison", "92", 78_000), ("Champigny-sur-Marne", "94", 77_000),
    ("Antibes", "06", 77_000), ("Fort-de-France", "972", 76_000), ("Pau", "64", 76_000), ("Saint-Maur-des-Fossés", "94", 75_000),
    ("Cannes", "06", 74_000), ("Mérignac", "33", 74_000), ("Ajaccio", "2A", 73_000), ("Drancy", "93", 72_000),
    ("Saint-Nazaire", "44", 72_000), ("Noisy-le-Grand", "93", 70_000), ("Évry-Courcouronnes", "91", 69_000),
    ("Issy-les-Moulineaux", "92", 68_000), ("Colmar", "68", 67_000), ("Calais", "62", 67_000), ("Vénissieux", "69", 67_000),
    ("Cergy", "95", 66_000), ("Levallois-Perret", "92", 66_000), ("Pessac", "33", 65_000), ("Valence", "26", 64_000),
    ("Bourges", "18", 64_000), ("Ivry-sur-Seine", "94", 64_000), ("Cayenne", "973", 63_000), ("Quimper", "29", 63_000),
    ("Clichy", "92", 63_000), ("La Seyne-sur-Mer", "83", 62_000), ("Antony", "92", 62_000), ("Troyes", "10", 62_000),
    ("Villeneuve-d'Ascq", "59", 62_000), ("Montauban", "82", 61_000), ("Neuilly-sur-Seine", "92", 59_000),
    ("Pantin", "93", 59_000), ("Niort", "79", 59_000), ("Chambéry", "73", 59_000), ("Sarcelles", "95", 58_000),
    ("Le Blanc-Mesnil", "93", 57_000), ("Lorient", "56", 57_000), ("Narbonne", "11", 56_000), ("Beauvais", "60", 56_000),
    ("Villejuif", "94", 56_000), ("Maisons-Alfort", "94", 56_000), ("Saint-André", "974", 56_000), ("Meaux", "77", 55_000),
    ("Hyères", "83", 55_000), ("Épinay-sur-Seine", "93", 55_000), ("Bobigny", "93", 55_000), ("La Roche-sur-Yon", "85", 55_000),
    ("Chelles", "77", 54_000), ("Vannes", "56", 54_000), ("Fréjus", "83", 54_000), ("Cholet", "49", 54_000),
    ("Bondy", "93", 54_000), ("Saint-Quentin", "02", 53_000), ("Clamart", "92", 53_000), ("Fontenay-sous-Bois", "94", 53_000),
    ("Cagnes-sur-Mer", "06", 52_000), ("Sartrouville", "78", 52_000), ("Bayonne", "64", 52_000), ("Saint-Ouen-sur-Seine", "93", 52_000),
    ("Vaulx-en-Velin", "69", 52_000), ("Corbeil-Essonnes", "91", 51_000), ("Arles", "13", 51_000), ("Laval", "53", 50_000),
    ("Grasse", "06", 50_000), ("Massy", "91", 50_000), ("Montrouge", "92", 50_000), ("Albi", "81", 49_000),
    ("Martigues", "13", 49_000), ("Suresnes", "92", 49_000), ("Vincennes", "94", 49_000), ("Saint-Herblain", "44", 49_000),
    ("Bastia", "2B", 48_000), ("Saint-Priest", "69", 47_000), ("Saint-Malo", "35", 47_000), ("Évreux", "27", 47_000),
    ("Belfort", "90", 46_000), ("Brive-la-Gaillarde", "19", 46_000), ("Blois", "41", 46_000), ("Meudon", "92", 46_000),
    ("Charleville-Mézières", "08", 46_000), ("Carcassonne", "11", 46_000), ("Saint-Germain-en-Laye", "78", 45_000),
    ("Puteaux", "92", 45_000), ("Alfortville", "94", 45_000), ("Chalon-sur-Saône", "71", 44_000), ("Sète", "34", 44_000),
    ("Saint-Brieuc", "22", 44_000), ("Châlons-en-Champagne", "51", 44_000), ("Caluire-et-Cuire", "69", 43_000),
    ("Bron", "69", 43_000), ("Talence", "33", 43_000), ("Châteauroux", "36", 43_000), ("Valenciennes", "59", 43_000),
    ("Tarbes", "65", 42_000), ("Angoulême", "16", 42_000), ("Alès", "30", 42_000), ("Rezé", "44", 42_000),
    ("Bourg-en-Bresse", "01", 42_000), ("Compiègne", "60", 41_000), ("Gap", "05", 41_000), ("Thionville", "57", 41_000),
    ("Arras", "62", 41_000), ("Anglet", "64", 40_000), ("Boulogne-sur-Mer", "62", 40_000), ("Montélimar", "26", 40_000),
    ("Douai", "59", 39_000), ("Chartres", "28", 38_000), ("Échirolles", "38", 37_000), ("Annemasse", "74", 37_000),
    ("Auxerre", "89", 35_000), ("Mâcon", "71", 34_000), ("Agen", "47", 33_000), ("Nevers", "58", 33_000),
    ("Lens", "62", 32_000), ("Mont-de-Marsan", "40", 30_000), ("Périgueux", "24", 30_000), ("Menton", "06", 30_000),
    ("Biarritz", "64", 25_000), ("Rodez", "12", 25_000), ("Épinal", "88", 31_000), ("Saint-Lô", "50", 19_000),
)

# ---- Other countries: (name, region, population, aliases) ---------------------------------------

_INTL: dict[str, tuple[tuple[str, str, int, tuple[str, ...]], ...]] = {
    "GB": (
        ("London", "England", 8_900_000, ("Londres",)), ("Birmingham", "England", 1_150_000, ()),
        ("Leeds", "England", 800_000, ()), ("Glasgow", "Scotland", 630_000, ()), ("Sheffield", "England", 560_000, ()),
        ("Manchester", "England", 550_000, ()), ("Edinburgh", "Scotland", 525_000, ("Édimbourg",)),
        ("Liverpool", "England", 490_000, ()), ("Bristol", "England", 470_000, ()), ("Leicester", "England", 370_000, ()),
        ("Cardiff", "Wales", 365_000, ()), ("Bradford", "England", 360_000, ()), ("Coventry", "England", 345_000, ()),
        ("Belfast", "Northern Ireland", 345_000, ()), ("Nottingham", "England", 325_000, ()),
        ("Newcastle upon Tyne", "England", 300_000, ("Newcastle",)), ("Brighton", "England", 280_000, ("Brighton and Hove",)),
        ("Plymouth", "England", 265_000, ()), ("Wolverhampton", "England", 265_000, ()), ("Derby", "England", 260_000, ()),
        ("Stoke-on-Trent", "England", 255_000, ()), ("Southampton", "England", 250_000, ()), ("Swansea", "Wales", 240_000, ()),
        ("Milton Keynes", "England", 230_000, ()), ("Portsmouth", "England", 210_000, ()), ("Aberdeen", "Scotland", 200_000, ()),
        ("York", "England", 200_000, ()), ("Bournemouth", "England", 190_000, ()), ("Reading", "England", 175_000, ()),
        ("Oxford", "England", 160_000, ()), ("Dundee", "Scotland", 150_000, ()), ("Cambridge", "England", 145_000, ()),
        ("Norwich", "England", 145_000, ()), ("Exeter", "England", 130_000, ()), ("Bath", "England", 95_000, ()),
    ),
    "US": (
        ("New York", "New York", 8_300_000, ("NYC", "New York City")), ("Los Angeles", "California", 3_850_000, ("LA",)),
        ("Chicago", "Illinois", 2_660_000, ()), ("Houston", "Texas", 2_300_000, ()), ("Phoenix", "Arizona", 1_650_000, ()),
        ("Philadelphia", "Pennsylvania", 1_560_000, ()), ("San Antonio", "Texas", 1_470_000, ()),
        ("San Diego", "California", 1_380_000, ()), ("Dallas", "Texas", 1_300_000, ()), ("Austin", "Texas", 980_000, ()),
        ("Jacksonville", "Florida", 970_000, ()), ("San Jose", "California", 970_000, ()), ("Fort Worth", "Texas", 960_000, ()),
        ("Columbus", "Ohio", 910_000, ()), ("Charlotte", "North Carolina", 900_000, ()), ("Indianapolis", "Indiana", 880_000, ()),
        ("San Francisco", "California", 810_000, ("SF",)), ("Seattle", "Washington", 750_000, ()),
        ("Denver", "Colorado", 715_000, ()), ("Nashville", "Tennessee", 690_000, ()),
        ("Washington", "District of Columbia", 680_000, ("Washington DC", "Washington D.C.", "DC")),
        ("Las Vegas", "Nevada", 660_000, ()), ("Boston", "Massachusetts", 650_000, ()), ("Portland", "Oregon", 630_000, ()),
        ("Detroit", "Michigan", 620_000, ()), ("Baltimore", "Maryland", 570_000, ()), ("Sacramento", "California", 525_000, ()),
        ("Kansas City", "Missouri", 510_000, ()), ("Atlanta", "Georgia", 500_000, ()), ("Raleigh", "North Carolina", 480_000, ()),
        ("Miami", "Florida", 450_000, ()), ("Minneapolis", "Minnesota", 425_000, ()), ("Tampa", "Florida", 400_000, ()),
        ("New Orleans", "Louisiana", 370_000, ()), ("Orlando", "Florida", 310_000, ()), ("Pittsburgh", "Pennsylvania", 300_000, ()),
        ("St. Louis", "Missouri", 290_000, ("Saint Louis",)), ("Salt Lake City", "Utah", 200_000, ()),
    ),
    "DE": (
        ("Berlin", "Berlin", 3_700_000, ()), ("Hamburg", "Hamburg", 1_900_000, ("Hambourg",)),
        ("München", "Bayern", 1_500_000, ("Munich", "Munchen")), ("Köln", "Nordrhein-Westfalen", 1_080_000, ("Cologne",)),
        ("Frankfurt am Main", "Hessen", 770_000, ("Frankfurt", "Francfort")), ("Stuttgart", "Baden-Württemberg", 630_000, ()),
        ("Düsseldorf", "Nordrhein-Westfalen", 620_000, ()), ("Leipzig", "Sachsen", 600_000, ()),
        ("Dortmund", "Nordrhein-Westfalen", 590_000, ()), ("Essen", "Nordrhein-Westfalen", 580_000, ()),
        ("Bremen", "Bremen", 570_000, ("Brême",)), ("Dresden", "Sachsen", 560_000, ("Dresde",)),
        ("Hannover", "Niedersachsen", 540_000, ("Hanover", "Hanovre")), ("Nürnberg", "Bayern", 520_000, ("Nuremberg",)),
        ("Duisburg", "Nordrhein-Westfalen", 500_000, ()), ("Bochum", "Nordrhein-Westfalen", 365_000, ()),
        ("Wuppertal", "Nordrhein-Westfalen", 355_000, ()), ("Bielefeld", "Nordrhein-Westfalen", 335_000, ()),
        ("Bonn", "Nordrhein-Westfalen", 330_000, ()), ("Münster", "Nordrhein-Westfalen", 315_000, ()),
        ("Mannheim", "Baden-Württemberg", 315_000, ()), ("Karlsruhe", "Baden-Württemberg", 305_000, ()),
        ("Augsburg", "Bayern", 300_000, ()), ("Wiesbaden", "Hessen", 280_000, ()),
        ("Mönchengladbach", "Nordrhein-Westfalen", 260_000, ()), ("Gelsenkirchen", "Nordrhein-Westfalen", 260_000, ()),
        ("Aachen", "Nordrhein-Westfalen", 250_000, ("Aix-la-Chapelle",)), ("Braunschweig", "Niedersachsen", 250_000, ()),
        ("Kiel", "Schleswig-Holstein", 245_000, ()), ("Freiburg im Breisgau", "Baden-Württemberg", 230_000, ("Freiburg",)),
        ("Mainz", "Rheinland-Pfalz", 220_000, ("Mayence",)), ("Potsdam", "Brandenburg", 185_000, ()),
        ("Heidelberg", "Baden-Württemberg", 160_000, ()), ("Regensburg", "Bayern", 155_000, ()),
    ),
    "ES": (
        ("Madrid", "Comunidad de Madrid", 3_330_000, ()), ("Barcelona", "Cataluña", 1_640_000, ("Barcelone",)),
        ("Valencia", "Comunitat Valenciana", 800_000, ("Valence (Espagne)",)), ("Sevilla", "Andalucía", 685_000, ("Seville", "Séville")),
        ("Zaragoza", "Aragón", 675_000, ("Saragosse",)), ("Málaga", "Andalucía", 580_000, ("Malaga",)),
        ("Murcia", "Región de Murcia", 460_000, ()), ("Palma", "Illes Balears", 420_000, ("Palma de Mallorca",)),
        ("Las Palmas de Gran Canaria", "Canarias", 380_000, ("Las Palmas",)), ("Bilbao", "País Vasco", 345_000, ()),
        ("Alicante", "Comunitat Valenciana", 340_000, ()), ("Córdoba", "Andalucía", 320_000, ("Cordoue",)),
        ("Valladolid", "Castilla y León", 300_000, ()), ("Vigo", "Galicia", 295_000, ()), ("Gijón", "Asturias", 270_000, ()),
        ("L'Hospitalet de Llobregat", "Cataluña", 265_000, ()), ("Vitoria-Gasteiz", "País Vasco", 255_000, ("Vitoria",)),
        ("A Coruña", "Galicia", 245_000, ("La Coruña",)), ("Elche", "Comunitat Valenciana", 235_000, ()),
        ("Granada", "Andalucía", 230_000, ("Grenade",)), ("Oviedo", "Asturias", 220_000, ()),
        ("Cartagena", "Región de Murcia", 215_000, ()), ("Santa Cruz de Tenerife", "Canarias", 210_000, ()),
        ("Pamplona", "Navarra", 205_000, ("Pampelune",)), ("San Sebastián", "País Vasco", 188_000, ("Donostia",)),
        ("Santander", "Cantabria", 172_000, ()), ("Marbella", "Andalucía", 150_000, ()), ("Salamanca", "Castilla y León", 145_000, ()),
        ("Tarragona", "Cataluña", 135_000, ()), ("Girona", "Cataluña", 103_000, ("Gérone",)),
    ),
    "IT": (
        ("Roma", "Lazio", 2_750_000, ("Rome",)), ("Milano", "Lombardia", 1_370_000, ("Milan",)),
        ("Napoli", "Campania", 920_000, ("Naples",)), ("Torino", "Piemonte", 850_000, ("Turin",)),
        ("Palermo", "Sicilia", 630_000, ("Palerme",)), ("Genova", "Liguria", 560_000, ("Genoa", "Gênes")),
        ("Bologna", "Emilia-Romagna", 390_000, ("Bologne",)), ("Firenze", "Toscana", 360_000, ("Florence",)),
        ("Bari", "Puglia", 315_000, ()), ("Catania", "Sicilia", 300_000, ("Catane",)), ("Verona", "Veneto", 255_000, ("Vérone",)),
        ("Venezia", "Veneto", 250_000, ("Venice", "Venise")), ("Messina", "Sicilia", 220_000, ()),
        ("Padova", "Veneto", 205_000, ("Padua", "Padoue")), ("Trieste", "Friuli-Venezia Giulia", 200_000, ()),
        ("Brescia", "Lombardia", 197_000, ()), ("Parma", "Emilia-Romagna", 195_000, ("Parme",)), ("Prato", "Toscana", 195_000, ()),
        ("Taranto", "Puglia", 190_000, ()), ("Modena", "Emilia-Romagna", 185_000, ("Modène",)),
        ("Reggio Calabria", "Calabria", 170_000, ()), ("Reggio Emilia", "Emilia-Romagna", 170_000, ()),
        ("Perugia", "Umbria", 162_000, ("Pérouse",)), ("Ravenna", "Emilia-Romagna", 155_000, ()),
        ("Livorno", "Toscana", 153_000, ("Livourne",)), ("Cagliari", "Sardegna", 150_000, ()),
        ("Monza", "Lombardia", 123_000, ()), ("Bergamo", "Lombardia", 120_000, ("Bergame",)),
        ("Trento", "Trentino-Alto Adige", 118_000, ("Trente",)), ("Vicenza", "Veneto", 110_000, ()),
    ),
    "BE": (
        ("Bruxelles", "Bruxelles-Capitale", 1_220_000, ("Brussels", "Brussel")),
        ("Antwerpen", "Flandre", 530_000, ("Anvers", "Antwerp")), ("Gent", "Flandre", 265_000, ("Gand", "Ghent")),
        ("Charleroi", "Wallonie", 202_000, ()), ("Liège", "Wallonie", 197_000, ("Luik", "Liege")),
        ("Schaerbeek", "Bruxelles-Capitale", 132_000, ()), ("Anderlecht", "Bruxelles-Capitale", 120_000, ()),
        ("Brugge", "Flandre", 119_000, ("Bruges",)), ("Namur", "Wallonie", 112_000, ("Namen",)),
        ("Leuven", "Flandre", 102_000, ("Louvain",)), ("Mons", "Wallonie", 95_000, ("Bergen",)),
        ("Aalst", "Flandre", 88_000, ("Alost",)), ("Mechelen", "Flandre", 87_000, ("Malines",)),
        ("Ixelles", "Bruxelles-Capitale", 87_000, ("Elsene",)), ("Uccle", "Bruxelles-Capitale", 84_000, ("Ukkel",)),
        ("La Louvière", "Wallonie", 81_000, ()), ("Hasselt", "Flandre", 79_000, ()), ("Sint-Niklaas", "Flandre", 79_000, ()),
        ("Kortrijk", "Flandre", 78_000, ("Courtrai",)), ("Oostende", "Flandre", 72_000, ("Ostende", "Ostend")),
        ("Tournai", "Wallonie", 69_000, ("Doornik",)), ("Genk", "Flandre", 67_000, ()), ("Seraing", "Wallonie", 64_000, ()),
        ("Roeselare", "Flandre", 64_000, ("Roulers",)), ("Mouscron", "Wallonie", 59_000, ("Moeskroen",)),
        ("Verviers", "Wallonie", 55_000, ()), ("Wavre", "Wallonie", 35_000, ("Waver",)),
        ("Ottignies-Louvain-la-Neuve", "Wallonie", 32_000, ("Louvain-la-Neuve",)), ("Waterloo", "Wallonie", 30_000, ()),
        ("Arlon", "Wallonie", 30_000, ()),
    ),
    "CH": (
        ("Zürich", "Zürich", 430_000, ("Zurich",)), ("Genève", "Genève", 205_000, ("Geneva", "Genf", "Geneve")),
        ("Basel", "Basel-Stadt", 175_000, ("Bâle", "Bale")), ("Lausanne", "Vaud", 140_000, ()),
        ("Bern", "Bern", 135_000, ("Berne",)), ("Winterthur", "Zürich", 117_000, ()), ("Luzern", "Luzern", 83_000, ("Lucerne",)),
        ("St. Gallen", "St. Gallen", 76_000, ("Saint-Gall", "St Gallen")), ("Lugano", "Ticino", 63_000, ()),
        ("Biel/Bienne", "Bern", 55_000, ("Biel", "Bienne")), ("Neuchâtel", "Neuchâtel", 45_000, ()),
        ("Thun", "Bern", 44_000, ("Thoune",)), ("Bellinzona", "Ticino", 43_000, ()), ("Köniz", "Bern", 42_000, ()),
        ("Fribourg", "Fribourg", 38_000, ("Freiburg im Üechtland",)), ("La Chaux-de-Fonds", "Neuchâtel", 37_000, ()),
        ("Schaffhausen", "Schaffhausen", 37_000, ("Schaffhouse",)), ("Chur", "Graubünden", 37_000, ("Coire",)),
        ("Vernier", "Genève", 36_000, ()), ("Uster", "Zürich", 36_000, ()), ("Sion", "Valais", 35_000, ("Sitten",)),
        ("Lancy", "Genève", 34_000, ()), ("Zug", "Zug", 31_000, ("Zoug",)), ("Yverdon-les-Bains", "Vaud", 30_000, ()),
        ("Montreux", "Vaud", 26_000, ()), ("Nyon", "Vaud", 22_000, ()), ("Aarau", "Aargau", 22_000, ()),
    ),
    "NL": (
        ("Amsterdam", "Noord-Holland", 920_000, ()), ("Rotterdam", "Zuid-Holland", 660_000, ()),
        ("Den Haag", "Zuid-Holland", 560_000, ("The Hague", "La Haye", "'s-Gravenhage")), ("Utrecht", "Utrecht", 370_000, ()),
        ("Eindhoven", "Noord-Brabant", 240_000, ()), ("Groningen", "Groningen", 235_000, ("Groningue",)),
        ("Tilburg", "Noord-Brabant", 225_000, ()), ("Almere", "Flevoland", 220_000, ()), ("Breda", "Noord-Brabant", 185_000, ()),
        ("Nijmegen", "Gelderland", 180_000, ("Nimègue",)), ("Apeldoorn", "Gelderland", 165_000, ()),
        ("Arnhem", "Gelderland", 165_000, ()), ("Haarlem", "Noord-Holland", 165_000, ()),
        ("Amersfoort", "Utrecht", 160_000, ()), ("Zaanstad", "Noord-Holland", 160_000, ()), ("Enschede", "Overijssel", 160_000, ()),
        ("'s-Hertogenbosch", "Noord-Brabant", 160_000, ("Den Bosch", "Bois-le-Duc")), ("Zwolle", "Overijssel", 132_000, ()),
        ("Leiden", "Zuid-Holland", 127_000, ("Leyde",)), ("Zoetermeer", "Zuid-Holland", 126_000, ()),
        ("Leeuwarden", "Friesland", 125_000, ()), ("Maastricht", "Limburg", 122_000, ()), ("Dordrecht", "Zuid-Holland", 121_000, ()),
        ("Alkmaar", "Noord-Holland", 110_000, ()), ("Delft", "Zuid-Holland", 105_000, ()), ("Hilversum", "Noord-Holland", 92_000, ()),
    ),
    "CA": (
        ("Toronto", "Ontario", 2_800_000, ()), ("Montréal", "Québec", 1_760_000, ("Montreal",)),
        ("Calgary", "Alberta", 1_300_000, ()), ("Ottawa", "Ontario", 1_020_000, ()), ("Edmonton", "Alberta", 1_010_000, ()),
        ("Winnipeg", "Manitoba", 750_000, ()), ("Mississauga", "Ontario", 720_000, ()),
        ("Vancouver", "British Columbia", 660_000, ()), ("Brampton", "Ontario", 660_000, ()), ("Hamilton", "Ontario", 570_000, ()),
        ("Surrey", "British Columbia", 570_000, ()), ("Québec", "Québec", 550_000, ("Quebec City", "Ville de Québec")),
        ("Halifax", "Nova Scotia", 440_000, ()), ("Laval", "Québec", 440_000, ()), ("London", "Ontario", 420_000, ()),
        ("Markham", "Ontario", 340_000, ()), ("Vaughan", "Ontario", 320_000, ()), ("Gatineau", "Québec", 290_000, ()),
        ("Saskatoon", "Saskatchewan", 270_000, ()), ("Kitchener", "Ontario", 260_000, ()), ("Longueuil", "Québec", 255_000, ()),
        ("Burnaby", "British Columbia", 250_000, ()), ("Windsor", "Ontario", 230_000, ()), ("Regina", "Saskatchewan", 230_000, ()),
        ("Oakville", "Ontario", 215_000, ()), ("Richmond", "British Columbia", 210_000, ()), ("Sherbrooke", "Québec", 172_000, ()),
        ("Kelowna", "British Columbia", 145_000, ()), ("Waterloo", "Ontario", 120_000, ()), ("Victoria", "British Columbia", 92_000, ()),
    ),
    "PT": (
        ("Lisboa", "Lisboa", 545_000, ("Lisbon", "Lisbonne")), ("Sintra", "Lisboa", 385_000, ()),
        ("Vila Nova de Gaia", "Porto", 305_000, ("Gaia",)), ("Porto", "Porto", 232_000, ("Oporto",)),
        ("Cascais", "Lisboa", 214_000, ()), ("Loures", "Lisboa", 201_000, ()), ("Braga", "Braga", 193_000, ()),
        ("Almada", "Setúbal", 177_000, ()), ("Amadora", "Lisboa", 175_000, ()), ("Matosinhos", "Porto", 172_000, ()),
        ("Oeiras", "Lisboa", 171_000, ()), ("Seixal", "Setúbal", 166_000, ()), ("Gondomar", "Porto", 164_000, ()),
        ("Guimarães", "Braga", 156_000, ()), ("Odivelas", "Lisboa", 148_000, ()), ("Coimbra", "Coimbra", 140_000, ()),
        ("Maia", "Porto", 135_000, ()), ("Leiria", "Leiria", 128_000, ()), ("Setúbal", "Setúbal", 123_000, ()),
        ("Funchal", "Madeira", 105_000, ()), ("Viseu", "Viseu", 99_000, ()), ("Viana do Castelo", "Viana do Castelo", 85_000, ()),
        ("Aveiro", "Aveiro", 80_000, ()), ("Faro", "Faro", 67_000, ()), ("Ponta Delgada", "Açores", 67_000, ()),
        ("Santarém", "Santarém", 58_000, ()), ("Évora", "Évora", 53_000, ()),
    ),
    "IE": (
        ("Dublin", "Leinster", 590_000, ("Baile Átha Cliath",)), ("Cork", "Munster", 225_000, ()),
        ("Limerick", "Munster", 102_000, ()), ("Galway", "Connacht", 86_000, ()), ("Waterford", "Munster", 60_000, ()),
        ("Drogheda", "Leinster", 44_000, ()), ("Dundalk", "Leinster", 43_000, ()), ("Swords", "Leinster", 40_000, ()),
        ("Bray", "Leinster", 33_000, ()), ("Navan", "Leinster", 33_000, ()), ("Kilkenny", "Leinster", 27_000, ()),
        ("Ennis", "Munster", 27_000, ()), ("Carlow", "Leinster", 27_000, ()), ("Dún Laoghaire", "Leinster", 26_000, ("Dun Laoghaire",)),
        ("Naas", "Leinster", 26_000, ()), ("Tralee", "Munster", 26_000, ()), ("Balbriggan", "Leinster", 25_000, ()),
        ("Portlaoise", "Leinster", 25_000, ()), ("Newbridge", "Leinster", 24_000, ()), ("Athlone", "Leinster", 22_000, ()),
        ("Mullingar", "Leinster", 22_000, ()), ("Letterkenny", "Ulster", 22_000, ()), ("Greystones", "Leinster", 22_000, ()),
        ("Wexford", "Leinster", 21_000, ()), ("Celbridge", "Leinster", 20_000, ()), ("Sligo", "Connacht", 20_000, ()),
        ("Clonmel", "Munster", 18_000, ()),
    ),
    "LU": (
        ("Luxembourg", "Luxembourg", 135_000, ("Luxembourg City", "Luxembourg-Ville", "Luxemburg")),
        ("Esch-sur-Alzette", "Esch-sur-Alzette", 37_000, ()), ("Differdange", "Esch-sur-Alzette", 29_000, ()),
        ("Dudelange", "Esch-sur-Alzette", 22_000, ()), ("Pétange", "Esch-sur-Alzette", 20_000, ()),
        ("Sanem", "Esch-sur-Alzette", 18_000, ()), ("Hesperange", "Luxembourg", 16_000, ()),
        ("Bettembourg", "Esch-sur-Alzette", 11_000, ()), ("Schifflange", "Esch-sur-Alzette", 11_000, ()),
        ("Ettelbruck", "Diekirch", 10_000, ()), ("Strassen", "Luxembourg", 10_000, ()), ("Mersch", "Mersch", 10_000, ()),
        ("Bertrange", "Luxembourg", 9_000, ()), ("Kayl", "Esch-sur-Alzette", 9_000, ()), ("Mamer", "Capellen", 9_000, ()),
        ("Junglinster", "Grevenmacher", 8_000, ()), ("Diekirch", "Diekirch", 7_000, ()), ("Wiltz", "Wiltz", 7_000, ()),
        ("Echternach", "Echternach", 6_000, ()), ("Grevenmacher", "Grevenmacher", 5_000, ()),
        ("Mondorf-les-Bains", "Remich", 5_000, ()), ("Clervaux", "Clervaux", 5_000, ()), ("Remich", "Remich", 4_000, ()),
        ("Sandweiler", "Luxembourg", 4_000, ()), ("Leudelange", "Esch-sur-Alzette", 3_000, ()),
    ),
    "AT": (
        ("Wien", "Wien", 1_980_000, ("Vienna", "Vienne")), ("Graz", "Steiermark", 300_000, ()),
        ("Linz", "Oberösterreich", 210_000, ()), ("Salzburg", "Salzburg", 155_000, ("Salzbourg",)),
        ("Innsbruck", "Tirol", 130_000, ()), ("Klagenfurt", "Kärnten", 103_000, ()), ("Villach", "Kärnten", 64_000, ()),
        ("Wels", "Oberösterreich", 63_000, ()), ("Sankt Pölten", "Niederösterreich", 56_000, ("St. Pölten",)),
        ("Dornbirn", "Vorarlberg", 50_000, ()), ("Wiener Neustadt", "Niederösterreich", 47_000, ()),
        ("Steyr", "Oberösterreich", 38_000, ()), ("Feldkirch", "Vorarlberg", 35_000, ()), ("Bregenz", "Vorarlberg", 30_000, ()),
        ("Leonding", "Oberösterreich", 29_000, ()), ("Klosterneuburg", "Niederösterreich", 27_000, ()),
        ("Baden", "Niederösterreich", 26_000, ()), ("Wolfsberg", "Kärnten", 25_000, ()), ("Leoben", "Steiermark", 25_000, ()),
        ("Krems an der Donau", "Niederösterreich", 25_000, ("Krems",)), ("Traun", "Oberösterreich", 25_000, ()),
        ("Amstetten", "Niederösterreich", 24_000, ()), ("Lustenau", "Vorarlberg", 24_000, ()),
        ("Kapfenberg", "Steiermark", 22_000, ()), ("Mödling", "Niederösterreich", 21_000, ()), ("Eisenstadt", "Burgenland", 15_000, ()),
    ),
    "SE": (
        ("Stockholm", "Stockholm", 985_000, ()), ("Göteborg", "Västra Götaland", 600_000, ("Gothenburg", "Goteborg")),
        ("Malmö", "Skåne", 360_000, ("Malmo",)), ("Uppsala", "Uppsala", 240_000, ()), ("Västerås", "Västmanland", 130_000, ()),
        ("Örebro", "Örebro", 130_000, ()), ("Linköping", "Östergötland", 120_000, ()), ("Helsingborg", "Skåne", 115_000, ()),
        ("Jönköping", "Jönköping", 100_000, ()), ("Norrköping", "Östergötland", 98_000, ()), ("Lund", "Skåne", 95_000, ()),
        ("Umeå", "Västerbotten", 91_000, ()), ("Gävle", "Gävleborg", 78_000, ()), ("Borås", "Västra Götaland", 75_000, ()),
        ("Södertälje", "Stockholm", 75_000, ()), ("Eskilstuna", "Södermanland", 70_000, ()), ("Halmstad", "Halland", 70_000, ()),
        ("Växjö", "Kronoberg", 70_000, ()), ("Karlstad", "Värmland", 67_000, ()), ("Sundsvall", "Västernorrland", 58_000, ()),
        ("Östersund", "Jämtland", 52_000, ()), ("Trollhättan", "Västra Götaland", 50_000, ()), ("Luleå", "Norrbotten", 49_000, ()),
        ("Kalmar", "Kalmar", 42_000, ()), ("Kristianstad", "Skåne", 42_000, ()), ("Skövde", "Västra Götaland", 40_000, ()),
    ),
    "DK": (
        ("København", "Hovedstaden", 650_000, ("Copenhagen", "Copenhague", "Kobenhavn")),
        ("Aarhus", "Midtjylland", 290_000, ("Århus",)), ("Odense", "Syddanmark", 182_000, ()),
        ("Aalborg", "Nordjylland", 120_000, ("Ålborg",)), ("Frederiksberg", "Hovedstaden", 104_000, ()),
        ("Gentofte", "Hovedstaden", 75_000, ()), ("Esbjerg", "Syddanmark", 72_000, ()), ("Randers", "Midtjylland", 63_000, ()),
        ("Kolding", "Syddanmark", 62_000, ()), ("Horsens", "Midtjylland", 61_000, ()), ("Vejle", "Syddanmark", 60_000, ()),
        ("Kongens Lyngby", "Hovedstaden", 56_000, ("Lyngby",)), ("Roskilde", "Sjælland", 52_000, ()),
        ("Herning", "Midtjylland", 50_000, ()), ("Silkeborg", "Midtjylland", 50_000, ()), ("Helsingør", "Hovedstaden", 47_000, ("Elsinore",)),
        ("Næstved", "Sjælland", 44_000, ()), ("Fredericia", "Syddanmark", 41_000, ()), ("Viborg", "Midtjylland", 41_000, ()),
        ("Køge", "Sjælland", 38_000, ()), ("Holstebro", "Midtjylland", 37_000, ()), ("Hillerød", "Hovedstaden", 36_000, ()),
        ("Taastrup", "Hovedstaden", 35_000, ()), ("Slagelse", "Sjælland", 34_000, ()), ("Sønderborg", "Syddanmark", 27_000, ()),
        ("Svendborg", "Syddanmark", 27_000, ()),
    ),
    "NO": (
        ("Oslo", "Oslo", 710_000, ()), ("Bergen", "Vestland", 290_000, ()), ("Trondheim", "Trøndelag", 215_000, ()),
        ("Stavanger", "Rogaland", 146_000, ()), ("Bærum", "Akershus", 128_000, ()), ("Kristiansand", "Agder", 115_000, ()),
        ("Drammen", "Buskerud", 103_000, ()), ("Asker", "Akershus", 98_000, ()), ("Lillestrøm", "Akershus", 90_000, ()),
        ("Fredrikstad", "Østfold", 84_000, ()), ("Sandnes", "Rogaland", 82_000, ()), ("Tromsø", "Troms", 78_000, ()),
        ("Ålesund", "Møre og Romsdal", 67_000, ()), ("Sandefjord", "Vestfold", 65_000, ()), ("Sarpsborg", "Østfold", 58_000, ()),
        ("Tønsberg", "Vestfold", 58_000, ()), ("Skien", "Telemark", 55_000, ()), ("Bodø", "Nordland", 53_000, ()),
        ("Moss", "Østfold", 50_000, ()), ("Larvik", "Vestfold", 48_000, ()), ("Arendal", "Agder", 45_000, ()),
        ("Haugesund", "Rogaland", 38_000, ()), ("Porsgrunn", "Telemark", 37_000, ()), ("Hamar", "Innlandet", 32_000, ()),
        ("Molde", "Møre og Romsdal", 32_000, ()),
    ),
    "FI": (
        ("Helsinki", "Uusimaa", 665_000, ("Helsingfors",)), ("Espoo", "Uusimaa", 310_000, ()),
        ("Tampere", "Pirkanmaa", 250_000, ()), ("Vantaa", "Uusimaa", 245_000, ()), ("Oulu", "Pohjois-Pohjanmaa", 212_000, ()),
        ("Turku", "Varsinais-Suomi", 200_000, ("Åbo",)), ("Jyväskylä", "Keski-Suomi", 147_000, ()),
        ("Kuopio", "Pohjois-Savo", 124_000, ()), ("Lahti", "Päijät-Häme", 120_000, ()), ("Pori", "Satakunta", 83_000, ()),
        ("Kouvola", "Kymenlaakso", 80_000, ()), ("Joensuu", "Pohjois-Karjala", 78_000, ()),
        ("Lappeenranta", "Etelä-Karjala", 73_000, ()), ("Hämeenlinna", "Kanta-Häme", 68_000, ()), ("Vaasa", "Pohjanmaa", 68_000, ()),
        ("Seinäjoki", "Etelä-Pohjanmaa", 65_000, ()), ("Rovaniemi", "Lappi", 64_000, ()), ("Mikkeli", "Etelä-Savo", 52_000, ()),
        ("Kotka", "Kymenlaakso", 51_000, ()), ("Salo", "Varsinais-Suomi", 51_000, ()), ("Porvoo", "Uusimaa", 51_000, ()),
        ("Kokkola", "Keski-Pohjanmaa", 48_000, ()), ("Hyvinkää", "Uusimaa", 47_000, ()), ("Lohja", "Uusimaa", 46_000, ()),
        ("Järvenpää", "Uusimaa", 45_000, ()),
    ),
    "PL": (
        ("Warszawa", "Mazowieckie", 1_860_000, ("Warsaw", "Varsovie")), ("Kraków", "Małopolskie", 800_000, ("Krakow", "Cracow", "Cracovie")),
        ("Wrocław", "Dolnośląskie", 675_000, ("Wroclaw", "Breslau")), ("Łódź", "Łódzkie", 655_000, ("Lodz",)),
        ("Poznań", "Wielkopolskie", 540_000, ("Poznan",)), ("Gdańsk", "Pomorskie", 485_000, ("Gdansk", "Dantzig")),
        ("Szczecin", "Zachodniopomorskie", 390_000, ()), ("Lublin", "Lubelskie", 335_000, ()),
        ("Bydgoszcz", "Kujawsko-Pomorskie", 330_000, ()), ("Białystok", "Podlaskie", 295_000, ("Bialystok",)),
        ("Katowice", "Śląskie", 285_000, ()), ("Gdynia", "Pomorskie", 245_000, ()), ("Częstochowa", "Śląskie", 210_000, ()),
        ("Radom", "Mazowieckie", 200_000, ()), ("Rzeszów", "Podkarpackie", 197_000, ("Rzeszow",)),
        ("Toruń", "Kujawsko-Pomorskie", 195_000, ("Torun",)), ("Sosnowiec", "Śląskie", 193_000, ()),
        ("Kielce", "Świętokrzyskie", 185_000, ()), ("Gliwice", "Śląskie", 175_000, ()), ("Olsztyn", "Warmińsko-Mazurskie", 170_000, ()),
        ("Bielsko-Biała", "Śląskie", 168_000, ()), ("Bytom", "Śląskie", 160_000, ()), ("Zabrze", "Śląskie", 155_000, ()),
        ("Zielona Góra", "Lubuskie", 140_000, ()), ("Rybnik", "Śląskie", 135_000, ()), ("Opole", "Opolskie", 127_000, ()),
        ("Sopot", "Pomorskie", 35_000, ()),
    ),
    "AU": (
        ("Sydney", "New South Wales", 5_300_000, ()), ("Melbourne", "Victoria", 5_100_000, ()),
        ("Brisbane", "Queensland", 2_600_000, ()), ("Perth", "Western Australia", 2_200_000, ()),
        ("Adelaide", "South Australia", 1_400_000, ()), ("Gold Coast", "Queensland", 700_000, ()),
        ("Newcastle", "New South Wales", 500_000, ()), ("Canberra", "Australian Capital Territory", 460_000, ()),
        ("Sunshine Coast", "Queensland", 350_000, ()), ("Central Coast", "New South Wales", 340_000, ()),
        ("Wollongong", "New South Wales", 310_000, ()), ("Geelong", "Victoria", 290_000, ()),
        ("Parramatta", "New South Wales", 260_000, ()), ("Hobart", "Tasmania", 250_000, ()), ("Townsville", "Queensland", 180_000, ()),
        ("Cairns", "Queensland", 155_000, ()), ("Darwin", "Northern Territory", 150_000, ()), ("Toowoomba", "Queensland", 140_000, ()),
        ("Ballarat", "Victoria", 110_000, ()), ("Bendigo", "Victoria", 100_000, ()), ("Launceston", "Tasmania", 90_000, ()),
        ("Mackay", "Queensland", 80_000, ()), ("Rockhampton", "Queensland", 80_000, ()), ("Bunbury", "Western Australia", 75_000, ()),
        ("Coffs Harbour", "New South Wales", 72_000, ()), ("Albury", "New South Wales", 55_000, ()),
    ),
    "MA": (
        ("Casablanca", "Casablanca-Settat", 3_360_000, ("Dar el Beida",)), ("Fès", "Fès-Meknès", 1_150_000, ("Fez", "Fes")),
        ("Salé", "Rabat-Salé-Kénitra", 980_000, ("Sale",)), ("Tanger", "Tanger-Tétouan-Al Hoceïma", 950_000, ("Tangier",)),
        ("Marrakech", "Marrakech-Safi", 930_000, ("Marrakesh",)), ("Meknès", "Fès-Meknès", 630_000, ("Meknes",)),
        ("Rabat", "Rabat-Salé-Kénitra", 580_000, ()), ("Oujda", "Oriental", 550_000, ()),
        ("Kénitra", "Rabat-Salé-Kénitra", 430_000, ("Kenitra",)), ("Agadir", "Souss-Massa", 420_000, ()),
        ("Tétouan", "Tanger-Tétouan-Al Hoceïma", 380_000, ("Tetouan",)), ("Témara", "Rabat-Salé-Kénitra", 310_000, ("Temara",)),
        ("Safi", "Marrakech-Safi", 310_000, ()), ("Mohammedia", "Casablanca-Settat", 210_000, ()),
        ("Khouribga", "Béni Mellal-Khénifra", 200_000, ()), ("El Jadida", "Casablanca-Settat", 195_000, ()),
        ("Béni Mellal", "Béni Mellal-Khénifra", 190_000, ("Beni Mellal",)), ("Nador", "Oriental", 160_000, ()),
        ("Taza", "Fès-Meknès", 140_000, ()), ("Settat", "Casablanca-Settat", 140_000, ()), ("Berrechid", "Casablanca-Settat", 135_000, ()),
        ("Khémisset", "Rabat-Salé-Kénitra", 130_000, ()), ("Inezgane", "Souss-Massa", 130_000, ()),
        ("Larache", "Tanger-Tétouan-Al Hoceïma", 125_000, ()), ("Essaouira", "Marrakech-Safi", 78_000, ()),
        ("Ouarzazate", "Drâa-Tafilalet", 72_000, ()),
    ),
}

# ---- Countries --------------------------------------------------------------------------------

# ISO → (English name, French name, other names/aliases)
COUNTRIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "FR": ("France", "France", ("République française", "French Republic", "Frankreich", "Francia")),
    "BE": ("Belgium", "Belgique", ("België", "Belgien", "Bélgica", "Belgio")),
    "CH": ("Switzerland", "Suisse", ("Schweiz", "Svizzera", "Suiza", "Swiss")),
    "LU": ("Luxembourg", "Luxembourg", ("Luxemburg", "Lëtzebuerg")),
    "DE": ("Germany", "Allemagne", ("Deutschland", "Alemania", "Germania")),
    "GB": ("United Kingdom", "Royaume-Uni", ("UK", "U.K.", "Great Britain", "Britain", "England", "Scotland", "Wales",
                                            "Northern Ireland", "Angleterre", "Grande-Bretagne", "Vereinigtes Königreich")),
    "US": ("United States", "États-Unis", ("USA", "U.S.", "U.S.A.", "United States of America", "America", "Amérique",
                                          "Etats-Unis", "Vereinigte Staaten", "Estados Unidos", "Stati Uniti")),
    "ES": ("Spain", "Espagne", ("España", "Spanien", "Spagna")),
    "IT": ("Italy", "Italie", ("Italia", "Italien")),
    "NL": ("Netherlands", "Pays-Bas", ("The Netherlands", "Holland", "Nederland", "Niederlande", "Países Bajos",
                                       "Paesi Bassi", "Hollande")),
    "PT": ("Portugal", "Portugal", ("Portogallo",)),
    "IE": ("Ireland", "Irlande", ("Éire", "Irland", "Irlanda", "Republic of Ireland")),
    "AT": ("Austria", "Autriche", ("Österreich", "Osterreich")),
    "SE": ("Sweden", "Suède", ("Sverige", "Schweden", "Suecia", "Svezia")),
    "DK": ("Denmark", "Danemark", ("Danmark", "Dänemark", "Dinamarca", "Danimarca")),
    "NO": ("Norway", "Norvège", ("Norge", "Norwegen", "Noruega", "Norvegia")),
    "FI": ("Finland", "Finlande", ("Suomi", "Finnland", "Finlandia")),
    "PL": ("Poland", "Pologne", ("Polska", "Polen", "Polonia")),
    "AU": ("Australia", "Australie", ("Australien",)),
    "MA": ("Morocco", "Maroc", ("Marokko", "Marruecos", "Marocco", "Al Maghrib")),
    "CA": ("Canada", "Canada", ("Kanada",)),
    "TN": ("Tunisia", "Tunisie", ("Tunesien", "Túnez")),
    "SN": ("Senegal", "Sénégal", ()),
    "DZ": ("Algeria", "Algérie", ("Algerien", "Argelia")),
    "CI": ("Côte d'Ivoire", "Côte d'Ivoire", ("Ivory Coast",)),
    "IL": ("Israel", "Israël", ()),
    "IN": ("India", "Inde", ("Indien",)),
    "SG": ("Singapore", "Singapour", ()),
    "AE": ("United Arab Emirates", "Émirats arabes unis", ("UAE", "Emirates", "Dubai")),
    "BR": ("Brazil", "Brésil", ("Brasil", "Brasilien")),
    "MX": ("Mexico", "Mexique", ("México", "Mexiko")),
    "NZ": ("New Zealand", "Nouvelle-Zélande", ()),
    "ZA": ("South Africa", "Afrique du Sud", ()),
    "CZ": ("Czechia", "Tchéquie", ("Czech Republic", "République tchèque")),
    "GR": ("Greece", "Grèce", ("Ellada",)),
    "RO": ("Romania", "Roumanie", ()),
    "HU": ("Hungary", "Hongrie", ()),
    "JP": ("Japan", "Japon", ()),
    "SA": ("Saudi Arabia", "Arabie saoudite", ()),
    "NG": ("Nigeria", "Nigéria", ()),
}

# Main search language per country (maps queries / search keywords / DDG locale).
COUNTRY_LANG: dict[str, str] = {
    "FR": "fr", "BE": "fr", "CH": "fr", "LU": "fr", "MA": "fr", "TN": "fr", "SN": "fr", "DZ": "fr", "CI": "fr",
    "GB": "en", "US": "en", "IE": "en", "AU": "en", "CA": "en", "NZ": "en", "IN": "en", "SG": "en", "ZA": "en", "NG": "en",
    "DE": "de", "AT": "de", "ES": "es", "MX": "es", "IT": "it", "NL": "nl", "PT": "pt", "BR": "pt",
    "SE": "sv", "DK": "da", "NO": "no", "FI": "fi", "PL": "pl",
}

# DuckDuckGo `kl` region codes.
_DDG_REGION: dict[str, str] = {
    "FR": "fr-fr", "BE": "be-fr", "CH": "ch-fr", "LU": "fr-fr", "DE": "de-de", "AT": "at-de", "GB": "uk-en",
    "US": "us-en", "IE": "ie-en", "AU": "au-en", "CA": "ca-en", "ES": "es-es", "IT": "it-it", "NL": "nl-nl",
    "PT": "pt-pt", "SE": "se-sv", "DK": "dk-da", "NO": "no-no", "FI": "fi-fi", "PL": "pl-pl", "NZ": "nz-en",
    "IN": "in-en", "SG": "sg-en", "ZA": "za-en", "MX": "mx-es", "BR": "br-pt", "IL": "il-en",
}


def _key(s: str) -> str:
    """Comparable key: unaccented, lowercase, punctuation-free; 'st'/'ste' expanded to 'saint'/'sainte'."""
    toks = normalize_key(s).split()
    return " ".join({"st": "saint", "ste": "sainte"}.get(t, t) for t in toks)


@lru_cache(maxsize=1)
def _country_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for code, (en, fr, others) in COUNTRIES.items():
        for name in (code, en, fr, *others):
            idx.setdefault(_key(name), code)
    return idx


def country_code(name: str | None) -> str | None:
    """'France' / 'Belgique' / 'Allemagne' / 'UK' / 'fr' → ISO alpha-2; None when unknown."""
    if not name:
        return None
    raw = name.strip()
    if len(raw) == 2 and raw.isalpha():
        up = raw.upper()
        if up == "UK":
            return "GB"
        if up in COUNTRIES:
            return up
    return _country_index().get(_key(raw))


def country_name(code: str, lang: str = "en") -> str:
    entry = COUNTRIES.get(code.upper())
    if entry is None:
        return code.upper()
    return entry[1] if lang == "fr" else entry[0]


def country_language(code: str | None) -> str:
    return COUNTRY_LANG.get((code or "").upper(), "en")


def ddg_region(code: str | None) -> str:
    return _DDG_REGION.get((code or "").upper(), "wt-wt")


# ---- Cities -----------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _all_cities() -> dict[str, tuple[City, ...]]:
    out: dict[str, list[City]] = {"FR": []}
    for name, dep, pop in _FR_CITIES:
        out["FR"].append(City(name, "FR", pop, dep, FR_REGIONS[FR_DEPARTMENTS[dep].region_code].name))
    for cc, rows in _INTL.items():
        out[cc] = [City(n, cc, p, None, r, a) for n, r, p, a in rows]
    return {cc: tuple(sorted(v, key=lambda c: -c.population)) for cc, v in out.items()}


@lru_cache(maxsize=1)
def _city_index() -> dict[str, list[City]]:
    idx: dict[str, list[City]] = {}
    for cities in _all_cities().values():
        for c in cities:
            for n in (c.name, *c.aliases):
                idx.setdefault(_key(n), []).append(c)
    for v in idx.values():
        # Mainland first for homonyms (Saint-Denis 93 vs 974), then by population.
        v.sort(key=lambda c: (bool(c.department and c.department.startswith("97")), -c.population))
    return idx


def known_countries() -> list[str]:
    """Countries with a city table."""
    return list(_all_cities().keys())


def country_cities(country: str) -> tuple[City, ...]:
    return _all_cities().get(country.upper(), ())


def find_city(name: str, country: str | None = None) -> City | None:
    """Look up a city by local name or alias (accent/case-insensitive). Largest match wins when ambiguous."""
    if not name:
        return None
    matches = _city_index().get(_key(name), [])
    if country:
        matches = [c for c in matches if c.country == country.upper()]
    return matches[0] if matches else None


def infer_countries(cities: Iterable[str] = (), regions: Iterable[str] = ()) -> list[str]:
    """Best-effort country inference from city / region names (no explicit country given)."""
    found: list[str] = []
    for r in regions:
        if _fr_region_departments(r):
            found.append("FR")
    for name in cities:
        c = find_city(name)
        if c is not None:
            found.append(c.country)
    return list(dict.fromkeys(found))


_POSTCODE_RE = re.compile(r"^\s*(\d{5})\s*$")
_REGION_CODE_RE = re.compile(r"^(?:REGION|REG|R)\s*[:\-]?\s*(\d{2})$")


def _fr_region_departments(name: str) -> tuple[str, ...]:
    """Departments for a FR region / historic region / department given by code or name."""
    raw = name.strip()
    if not raw:
        return ()
    up = raw.upper()
    if up in FR_DEPARTMENTS:
        return (up,)
    if raw.isdigit() and raw.zfill(2) in FR_DEPARTMENTS:
        return (raw.zfill(2),)
    k = _key(raw)
    # Bare numeric region codes collide with department codes ("84" = Vaucluse): regions use "R84" / "region 84".
    m = _REGION_CODE_RE.match(up)
    reg_code = m.group(1) if m else None
    for code, reg in FR_REGIONS.items():
        if k in {_key(reg.name), *(_key(a) for a in reg.aliases)} or reg_code == code:
            return tuple(d.code for d in FR_DEPARTMENTS.values() if d.region_code == code)
    for hist, deps in FR_HISTORIC_REGIONS.items():
        if k == _key(hist):
            return deps
    for d in FR_DEPARTMENTS.values():
        if k == _key(d.name):
            return (d.code,)
    return ()


def _fr_city_department(name: str) -> str | None:
    m = _POSTCODE_RE.match(name)
    if m:
        code = m.group(1)
        if code.startswith("97"):
            return code[:3]
        if code.startswith("20"):
            return "2A" if int(code) < 20200 else "2B"
        return code[:2]
    c = find_city(re.sub(r"\s+\d{1,2}(e|er|eme|ème)?$", "", name.strip()), "FR")
    return c.department if c else None


def _by_priority(codes: Iterable[str]) -> list[str]:
    wanted = set(codes)
    return [c for c in DEPARTMENTS_BY_ECONOMIC_SIZE if c in wanted]


def departments_for(regions: Sequence[str], cities: Sequence[str]) -> list[str]:
    """FR department codes covering the requested regions (names/codes/aliases, departments) and cities.

    Ordered by economic size. Empty when nothing French could be resolved (= no geographic restriction).
    """
    codes: list[str] = []
    for r in regions:
        codes.extend(_fr_region_departments(r))
    for c in cities:
        dep = _fr_city_department(c)
        if dep:
            codes.append(dep)
    return _by_priority(codes)


def region_for_department(code: str) -> Region | None:
    d = FR_DEPARTMENTS.get(code)
    return d.region if d else None


def cities_for(
    country: str,
    *,
    regions: Sequence[str] = (),
    cities: Sequence[str] = (),
    expansion: int = 0,
) -> list[City]:
    """Cities to query for a country, respecting the requested area.

    * explicit cities first (unknown names are kept as-is); for France, expansion adds smaller cities of the
      same departments (the metro area) — never outside the requested area;
    * regions: the region's largest cities, ``expansion`` adds smaller ones;
    * country only: the largest cities, ``expansion`` adds smaller ones.
    """
    cc = country.upper()
    table = country_cities(cc)
    out: list[City] = []
    for name in cities:
        c = find_city(name, cc)
        out.append(c if c is not None else City(name.strip(), cc))
    region_pool: list[City] = []
    if regions:
        if cc == "FR":
            deps = {d for r in regions for d in _fr_region_departments(r)}
            region_pool = [c for c in table if c.department in deps]
        else:
            keys = {_key(r) for r in regions}
            region_pool = [c for c in table if c.region and _key(c.region) in keys]
    if cities:
        if expansion > 0 and cc == "FR":
            deps = {c.department for c in out if c.department}
            extra = [c for c in table if c.department in deps and c not in out]
            out.extend(extra[: 6 * expansion])
        if region_pool:
            out.extend(c for c in region_pool[: 8 + 12 * expansion] if c not in out)
        return out
    if regions:
        return region_pool[: 8 + 12 * expansion]
    return list(table[: 10 + 15 * expansion])
