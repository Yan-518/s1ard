import os
import re
import time
from lxml import etree
from pathlib import Path
import dateutil.parser
from datetime import datetime, timedelta
from pystac_client import Client
import pystac_client.exceptions
from spatialist import Vector, crsConvert
import asf_search as asf
from pyroSAR import identify_many, ID
from S1_NRB.ancillary import buffer_time
from S1_NRB.tile_extraction import aoi_from_tile, tile_from_aoi


class ASF(ID):
    def __init__(self, meta):
        self.scene = meta['properties']['url']
        self.meta = self.scanMetadata(meta)
        super(ASF, self).__init__(self.meta)
    
    def scanMetadata(self, meta):
        out = dict()
        out['acquisition_mode'] = meta['properties']['beamModeType']
        out['coordinates'] = [tuple(x) for x in meta['geometry']['coordinates'][0]]
        out['frameNumber'] = meta['properties']['frameNumber']
        out['orbit'] = meta['properties']['flightDirection'][0]
        out['orbitNumber_abs'] = meta['properties']['orbit']
        out['orbitNumber_rel'] = meta['properties']['pathNumber']
        out['polarizations'] = meta['properties']['polarization'].split('+')
        product = meta['properties']['processingLevel']
        out['product'] = re.search('(?:GRD|SLC|SM|OCN)', product).group()
        out['projection'] = crsConvert(4326, 'wkt')
        out['sensor'] = meta['properties']['platform'].replace('entinel-', '')
        start = meta['properties']['startTime']
        stop = meta['properties']['stopTime']
        out['start'] = datetime.strptime(start, '%Y-%m-%dT%H:%M:%S.%fZ').strftime('%Y%m%dT%H%M%S')
        out['stop'] = datetime.strptime(stop, '%Y-%m-%dT%H:%M:%S.%fZ').strftime('%Y%m%dT%H%M%S')
        out['spacing'] = -1
        out['samples'] = -1
        out['lines'] = -1
        out['cycleNumber'] = -1
        out['sliceNumber'] = -1
        out['totalSlices'] = -1
        return out


class STACArchive(object):
    """
    Search for scenes in a SpatioTemporal Asset Catalog.
    Scenes are expected to be unpacked with a folder suffix .SAFE.
    The interface is kept consistent with :class:`pyroSAR.drivers.Archive`.

    Parameters
    ----------
    url: str
        the catalog URL
    collections: str or list[str]
        the catalog collection(s) to be searched
    """
    
    def __init__(self, url, collections):
        self.url = url
        self.max_tries = 300
        self._open_catalog()
        if isinstance(collections, str):
            self.collections = [collections]
        elif isinstance(collections, list):
            self.collections = collections
        else:
            raise TypeError("'collections' must be of type str or list")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
    
    @staticmethod
    def _get_proc_time(scene):
        with open(os.path.join(scene, 'manifest.safe'), 'rb') as f:
            tree = etree.fromstring(f.read())
        proc = tree.find(path='.//xmlData/safe:processing',
                         namespaces=tree.nsmap)
        start = proc.attrib['start']
        del tree, proc
        return datetime.strptime(start, '%Y-%m-%dT%H:%M:%S.%f')
    
    def _filter_duplicates(self, scenes):
        tmp = sorted(scenes)
        pattern = '([0-9A-Z_]{16})_([0-9T]{15})_([0-9T]{15})'
        keep = []
        i = 0
        while i < len(tmp):
            group = [tmp[i]]
            match1 = re.search(pattern, os.path.basename(tmp[i])).groups()
            j = i + 1
            while j < len(tmp):
                match2 = re.search(pattern, os.path.basename(tmp[j])).groups()
                if match1 == match2:
                    group.append(tmp[j])
                    j += 1
                else:
                    break
            if len(group) > 1:
                tproc = [self._get_proc_time(x) for x in group]
                keep.append(group[tproc.index(max(tproc))])
            else:
                keep.append(group[0])
            i = j
        return keep
    
    def _open_catalog(self):
        i = 1
        while True:
            try:
                self.catalog = Client.open(self.url)
                # print('catalog opened successfully')
                break
            except pystac_client.exceptions.APIError:
                # print(f'failed opening the catalog at try {i:03d}/{self.max_tries}')
                if i < self.max_tries:
                    i += 1
                    time.sleep(1)
                else:
                    raise
    
    def close(self):
        del self.catalog
    
    def select(self, sensor=None, product=None, acquisition_mode=None,
               mindate=None, maxdate=None, frameNumber=None,
               vectorobject=None, date_strict=True, check_exist=True):
        """
        Select scenes from the catalog. Used STAC keys:
        
        - platform
        - start_datetime
        - end_datetime
        - sar:instrument_mode
        - sar:product_type
        - s1:datatake (custom)

        Parameters
        ----------
        sensor: str or list[str] or None
            S1A or S1B
        product: str or list[str] or None
            GRD or SLC
        acquisition_mode: str or list[str] or None
            IW, EW or SM
        mindate: str or datetime.datetime or None
            the minimum acquisition date
        maxdate: str or datetime.datetime or None
            the maximum acquisition date
        frameNumber: int or list[int] or None
            the data take ID in decimal representation.
            Requires custom STAC key `s1:datatake`.
        vectorobject: spatialist.vector.Vector or None
            a geometry with which the scenes need to overlap
        date_strict: bool
            treat dates as strict limits or also allow flexible limits to incorporate scenes
            whose acquisition period overlaps with the defined limit?
            
            - strict: start >= mindate & stop <= maxdate
            - not strict: stop >= mindate & start <= maxdate
        check_exist: bool
            check whether found files exist locally?

        Returns
        -------
        list[str]
            the locations of the scene directories with suffix .SAFE
        
        See Also
        --------
        pystac_client.Client.search
        """
        pars = locals()
        del pars['date_strict']
        del pars['check_exist']
        del pars['self']
        
        lookup = {'product': 'sar:product_type',
                  'acquisition_mode': 'sar:instrument_mode',
                  'mindate': 'start_datetime',
                  'maxdate': 'end_datetime',
                  'sensor': 'platform',
                  'frameNumber': 's1:datatake'}
        lookup_platform = {'S1A': 'sentinel-1a',
                           'S1B': 'sentinel-1b'}
        
        flt = {'op': 'and', 'args': []}
        
        for key in pars.keys():
            val = pars[key]
            if val is None:
                continue
            if key == 'mindate':
                if isinstance(val, datetime):
                    val = datetime.strftime(val, '%Y%m%dT%H%M%S')
                if date_strict:
                    arg = {'op': '>=', 'args': [{'property': 'start_datetime'}, val]}
                else:
                    arg = {'op': '>=', 'args': [{'property': 'end_datetime'}, val]}
            elif key == 'maxdate':
                if isinstance(val, datetime):
                    val = datetime.strftime(val, '%Y%m%dT%H%M%S')
                if date_strict:
                    arg = {'op': '<=', 'args': [{'property': 'end_datetime'}, val]}
                else:
                    arg = {'op': '<=', 'args': [{'property': 'start_datetime'}, val]}
            elif key == 'vectorobject':
                if isinstance(val, Vector):
                    with val.clone() as vec:
                        vec.reproject(4326)
                        ext = vec.extent
                        arg = {'op': 's_intersects',
                               'args': [{'property': 'geometry'},
                                        {'type': 'Polygon',
                                         'coordinates': [[[ext['xmin'], ext['ymin']],
                                                          [ext['xmin'], ext['ymax']],
                                                          [ext['xmax'], ext['ymax']],
                                                          [ext['xmax'], ext['ymin']],
                                                          [ext['xmin'], ext['ymin']]]]}],
                               }
                else:
                    raise TypeError('argument vectorobject must be of type spatialist.vector.Vector')
            else:
                args = []
                if isinstance(val, (str, int)):
                    val = [val]
                for v in val:
                    if key == 'sensor':
                        value = lookup_platform[v]
                    elif key == 'frameNumber':
                        value = '{:06X}'.format(v)  # convert to hexadecimal
                    else:
                        value = v
                    a = {'op': '=', 'args': [{'property': lookup[key]}, value]}
                    args.append(a)
                if len(args) == 1:
                    arg = args[0]
                else:
                    arg = {'op': 'or', 'args': args}
            flt['args'].append(arg)
        t = 1
        while True:
            try:
                result = self.catalog.search(collections=self.collections,
                                             filter=flt, max_items=None)
                result = list(result.items())
                # print('catalog search successful')
                break
            except pystac_client.exceptions.APIError:
                # print(f'failed searching the catalog at try {t:03d}/{self.max_tries}')
                if t < self.max_tries:
                    t += 1
                    time.sleep(1)
                else:
                    raise
        out = []
        for item in result:
            assets = item.assets
            ref = assets[list(assets.keys())[0]]
            href = ref.href
            path = href[:re.search(r'\.SAFE', href).end()]
            path = re.sub('^file://', '', path)
            if Path(path).exists():
                path = os.path.realpath(path)
            else:
                if check_exist:
                    raise RuntimeError('scene does not exist locally:', path)
            out.append(path)
        out = self._filter_duplicates(out)
        return out


class ASFArchive(object):
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        return
    
    @staticmethod
    def select(sensor=None, product=None, acquisition_mode=None, mindate=None,
               maxdate=None, vectorobject=None, date_strict=True, return_value='url'):
        """
        Select scenes from the ASF catalog.

        Parameters
        ----------
        sensor: str or list[str] or None
            S1A or S1B
        product: str or list[str] or None
            GRD or SLC
        acquisition_mode: str or list[str] or None
            IW, EW or SM
        mindate: str or datetime.datetime or None
            the minimum acquisition date
        maxdate: str or datetime.datetime or None
            the maximum acquisition date
        vectorobject: spatialist.vector.Vector or None
            a geometry with which the scenes need to overlap
        date_strict: bool
            treat dates as strict limits or also allow flexible limits to incorporate scenes
            whose acquisition period overlaps with the defined limit?
            
            - strict: start >= mindate & stop <= maxdate
            - not strict: stop >= mindate & start <= maxdate
        return_value: str or list[str] or ASF
            the metadata return value; see :func:`~S1_NRB.search.asf_select` for details
        
        Returns
        -------
        list[str] or list[tuple[str]] or list[ASF]
            the scene metadata attributes as specified with `return_value`;
            see :func:`~S1_NRB.search.asf_select` for details
        """
        return asf_select(sensor, product, acquisition_mode, mindate, maxdate, vectorobject,
                          return_value=return_value, date_strict=date_strict)


def asf_select(sensor, product, acquisition_mode, mindate, maxdate,
               vectorobject=None, return_value='url', date_strict=True):
    """
    Search scenes in the Alaska Satellite Facility (ASF) data catalog using the
    `asf_search <https://github.com/asfadmin/Discovery-asf_search>`_ package.
    This simplified function is solely intended for cross-checking an online catalog in
    :func:`~S1_NRB.search.check_acquisition_completeness`.
    
    Parameters
    ----------
    sensor: str
        S1A or S1B
    product: str
        GRD or SLC
    acquisition_mode: str
        IW, EW or SM
    mindate: str or datetime.datetime
        the minimum acquisition date
    maxdate: str or datetime.datetime
        the maximum acquisition date
    vectorobject: spatialist.vector.Vector or None
        a geometry with which the scenes need to overlap
    return_value: str or list[str] or ASF
        the metadata return value; if ASF, a :class:`~S1_NRB.search.ASF` object is returned;
        string options specify certain properties to return: `beamModeType`, `browse`, `bytes`, `centerLat`, `centerLon`,
        `faradayRotation`, `fileID`, `flightDirection`, `groupID`, `granuleType`, `insarStackId`, `md5sum`,
        `offNadirAngle`, `orbit`, `pathNumber`, `platform`, `pointingAngle`, `polarization`, `processingDate`,
        `processingLevel`, `sceneName`, `sensor`, `startTime`, `stopTime`, `url`, `pgeVersion`, `fileName`,
        `frameNumber`
    date_strict: bool
        treat dates as strict limits or also allow flexible limits to incorporate scenes
        whose acquisition period overlaps with the defined limit?
        
        - strict: start >= mindate & stop <= maxdate
        - not strict: stop >= mindate & start <= maxdate

    Returns
    -------
    list[str] or list[tuple[str]] or list[ASF]
        the scene metadata attributes as specified with `return_value`; the return type is a list of strings,
        tuples or :class:`~S1_NRB.search.ASF` objects depending on whether `return_type` is of type string, list or :class:`~S1_NRB.search.ASF`.
    
    """
    if product == 'GRD':
        processing_level = ['GRD_HD', 'GRD_MD', 'GRD_MS', 'GRD_HS', 'GRD_FD']
    else:
        processing_level = product
    if acquisition_mode == 'SM':
        beam_mode = ['S1', 'S2', 'S3', 'S4', 'S5', 'S6']
    else:
        beam_mode = acquisition_mode
    if vectorobject is not None:
        with vectorobject.clone() as geom:
            geom.reproject(4326)
            geometry = geom.convert2wkt(set3D=False)[0]
    else:
        geometry = None
    
    if isinstance(mindate, str):
        start = dateutil.parser.parse(mindate)
    else:
        start = mindate
    
    if isinstance(maxdate, str):
        stop = dateutil.parser.parse(maxdate)
    else:
        stop = maxdate
    
    result = asf.search(platform=sensor.replace('S1', 'Sentinel-1'),
                        processingLevel=processing_level,
                        beamMode=beam_mode,
                        start=start,
                        end=stop,
                        intersectsWith=geometry).geojson()
    features = result['features']
    
    def date_extract(item, key):
        value = item['properties'][key]
        out = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ')
        return out
    
    if date_strict:
        features = [x for x in features
                    if start <= date_extract(x, 'startTime')
                    and date_extract(x, 'stopTime') <= stop]
    
    if return_value is ASF:
        return [ASF(x) for x in features]
    out = []
    for item in features:
        properties = item['properties']
        if isinstance(return_value, str):
            out.append(properties[return_value])
        elif isinstance(return_value, list):
            out.append(tuple([properties[x] for x in return_value]))
        else:
            raise TypeError(f'invalid type of return value: {type(return_value)}')
    return sorted(out)


def scene_select(archive, kml_file, aoi_tiles=None, aoi_geometry=None, **kwargs):
    """
    Central scene search utility. Selects scenes from a database and returns their file names
    together with the MGRS tile names for which to process ARD products.
    The list of MGRS tile names is either identical to the list provided with `aoi_tiles`,
    the list of all tiles overlapping with `aoi_geometry`, or the list of all tiles overlapping
    with an initial scene search result if no geometry has been defined via `aoi_tiles` or
    `aoi_geometry`. In the latter case, the search procedure is as follows:
    
     - perform a first search matching all other search parameters
     - derive all MGRS tile geometries overlapping with the selection
     - derive the minimum and maximum acquisition times of the selection as search parameters
       `mindate` and `maxdate`
     - extend the `mindate` and `maxdate` search parameters by one minute
     - perform a second search with the extended acquisition date parameters and the
       derived MGRS tile geometries
    
    As consequence, if one defines the search parameters to only return one scene, the neighboring
    acquisitions will also be returned. This is because the scene overlaps with a set of MGRS
    tiles of which many or all will also overlap with these neighboring acquisitions. To ensure
    full coverage of all MGRS tiles, the neighbors of the scene in focus have to be processed too.
    
    Parameters
    ----------
    archive: pyroSAR.drivers.Archive or STACArchive or ASFArchive
        an open scene archive connection
    kml_file: str
        the KML file containing the MGRS tile geometries.
    aoi_tiles: list[str] or None
        a list of MGRS tile names for spatial search
    aoi_geometry: str or None
        the name of a vector geometry file for spatial search
    kwargs
        further search arguments passed to :meth:`pyroSAR.drivers.Archive.select`
        or :meth:`STACArchive.select` or :meth:`ASFArchive.select`

    Returns
    -------
    tuple[list[str], list[str]]
    
     - the list of scenes
     - the list of MGRS tiles
    
    """
    args = kwargs.copy()
    for key in ['acquisition_mode']:
        if key not in args.keys():
            args[key] = None
    
    if args['acquisition_mode'] == 'SM':
        args['acquisition_mode'] = ('S1', 'S2', 'S3', 'S4', 'S5', 'S6')
    
    if isinstance(archive, ASFArchive):
        args['return_value'] = ASF
    
    vec = None
    selection = []
    if aoi_tiles is not None:
        vec = aoi_from_tile(kml=kml_file, tile=aoi_tiles)
        if not isinstance(vec, list):
            vec = [vec]
    elif aoi_geometry is not None:
        vec = [Vector(aoi_geometry)]
        aoi_tiles = tile_from_aoi(vector=vec[0], kml=kml_file)
    
    # derive geometries and tiles from scene footprints
    if vec is None:
        selection_tmp = archive.select(vectorobject=vec, **args)
        if not isinstance(selection_tmp[0], ID):
            scenes = identify_many(scenes=selection_tmp, sortkey='start')
        else:
            scenes = selection_tmp
        scenes_geom = [x.geometry() for x in scenes]
        # select all tiles overlapping with the scenes for further processing
        vec = tile_from_aoi(vector=scenes_geom, kml=kml_file,
                            return_geometries=True)
        aoi_tiles = [x.mgrs for x in vec]
        del scenes_geom
        
        args['mindate'] = min([datetime.strptime(x.start, '%Y%m%dT%H%M%S') for x in scenes])
        args['maxdate'] = max([datetime.strptime(x.stop, '%Y%m%dT%H%M%S') for x in scenes])
        del scenes
        # extend the time range to fully cover all tiles
        # (one additional scene needed before and after each data take group)
        args['mindate'] -= timedelta(minutes=1)
        args['maxdate'] += timedelta(minutes=1)
    
    if isinstance(archive, ASFArchive):
        args['return_value'] = 'url'
    
    for item in vec:
        selection.extend(
            archive.select(vectorobject=item, **args))
    del vec
    return sorted(list(set(selection))), aoi_tiles


def collect_neighbors(archive, scene):
    """
    Collect a scene's neighboring acquisitions in a data take
    
    Parameters
    ----------
    archive: pyroSAR.drivers.Archive or STACArchive or ASFArchive
        an open scene archive connection
    scene: pyroSAR.drivers.ID
        the Sentinel-1 scene to be checked

    Returns
    -------
    list[str]
        the file names of the neighboring scenes
    """
    start, stop = buffer_time(scene.start, scene.stop, seconds=2)
    
    neighbors = archive.select(mindate=start, maxdate=stop, date_strict=False,
                               sensor=scene.sensor, product=scene.product,
                               acquisition_mode=scene.acquisition_mode)
    del neighbors[neighbors.index(scene.scene)]
    return neighbors


def check_acquisition_completeness(archive, scenes):
    """
    Check presence of neighboring acquisitions.
    Check that for each scene a predecessor and successor can be queried
    from the database unless the scene is at the start or end of the data take.
    This ensures that no scene that could be covering an area of interest is missed
    during processing. In case a scene is suspected to be missing, the Alaska Satellite Facility (ASF)
    online catalog is cross-checked.
    An error will only be raised if the locally missing scene is present in the ASF catalog.

    Parameters
    ----------
    archive: pyroSAR.drivers.Archive or STACArchive
        an open scene archive connection
    scenes: list[pyroSAR.drivers.ID]
        a list of scenes

    Returns
    -------

    Raises
    ------
    RuntimeError

    See Also
    --------
    S1_NRB.search.asf_select
    """
    messages = []
    for scene in scenes:
        slice = scene.meta['sliceNumber']
        n_slices = scene.meta['totalSlices']
        groupsize = 3
        has_successor = True
        has_predecessor = True
        
        start, stop = buffer_time(scene.start, scene.stop, seconds=2)
        ref = None
        if slice == 0 or n_slices == 0:
            # NRT slicing mode
            ref = asf_select(sensor=scene.sensor,
                             product=scene.product,
                             acquisition_mode=scene.acquisition_mode,
                             mindate=start,
                             maxdate=stop,
                             return_value='sceneName')
            match = [re.search(scene.pattern, x + '.SAFE').groupdict() for x in ref]
            ref_start_min = min([x['start'] for x in match])
            ref_stop_max = max([x['stop'] for x in match])
            if ref_start_min == scene.start:
                groupsize -= 1
                has_predecessor = False
            if ref_stop_max == scene.stop:
                groupsize -= 1
                has_successor = False
        else:
            if slice == 1:  # first slice in the data take
                groupsize -= 1
                has_predecessor = False
            if slice == n_slices:  # last slice in the data take
                groupsize -= 1
                has_successor = False
        # Do another database selection to get the scene in question as well as its potential
        # predecessor and successor by adding an acquisition time buffer of two seconds.
        group = archive.select(sensor=scene.sensor,
                               product=scene.product,
                               acquisition_mode=scene.acquisition_mode,
                               mindate=start,
                               maxdate=stop,
                               date_strict=False)
        group = identify_many(group)
        # if the number of selected scenes is lower than the expected group size,
        # check whether the predecessor, the successor or both are missing by
        # cross-checking with the ASF database.
        if len(group) < groupsize:
            if ref is None:
                ref = asf_select(sensor=scene.sensor,
                                 product=scene.product,
                                 acquisition_mode=scene.acquisition_mode,
                                 mindate=start,
                                 maxdate=stop,
                                 return_value='sceneName')
            match = [re.search(scene.pattern, x + '.SAFE').groupdict() for x in ref]
            ref_start_min = min([x['start'] for x in match])
            ref_stop_max = max([x['stop'] for x in match])
            start_min = min([x.start for x in group])
            stop_max = max([x.stop for x in group])
            missing = []
            if ref_start_min < start < start_min and has_predecessor:
                missing.append('predecessor')
            if stop_max < stop < ref_stop_max and has_successor:
                missing.append('successor')
            if len(missing) > 0:
                base = os.path.basename(scene.scene)
                messages.append(f'{" and ".join(missing)} acquisition for scene {base}')
    if len(messages) != 0:
        text = '\n - '.join(messages)
        raise RuntimeError(f'missing the following scenes:\n - {text}')
