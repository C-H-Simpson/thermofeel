# (C) Copyright 1996- ECMWF.
#
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.


import math
import sys
from datetime import datetime, timedelta, timezone

import eccodes
import numpy as np

import thermofeel as thermofeel

UTCI_MIN_VALUE = thermofeel.celsius_to_kelvin(-80)
UTCI_MAX_VALUE = thermofeel.celsius_to_kelvin(90)
MISSING_VALUE = -9999.0

############################################################################################################


def field_stats(name, values):
    print(
        f"{name} avg {np.nanmean(values)} max {np.nanmax(values)} "
        f"min {np.nanmin(values)} stddev {np.nanstd(values, dtype=np.float64)}"
    )


############################################################################################################

lats = None
lons = None


def decode_grib(fpath):

    # print(f"decoding file {fpath}")

    prev_step = None
    prev_number = None

    msgcount = 0
    messages = {}

    with open(fpath, "rb") as f:

        while True:

            msg = eccodes.codes_any_new_from_file(f)

            if msg is None:  # end of file, stop iterating
                # print(f"yielding {len(messages)} messages")
                yield messages
                for k, m in messages.items():
                    grib = m["grib"]
                    eccodes.codes_release(grib)
                messages = {}
                break

            md = dict()
            msgcount += 1

            # loop metadata key-values
            it = eccodes.codes_keys_iterator_new(msg, "mars")
            while eccodes.codes_keys_iterator_next(it):
                k = eccodes.codes_keys_iterator_get_name(it)
                v = eccodes.codes_get_string(msg, k)
                md[k] = v
            eccodes.codes_keys_iterator_delete(it)

            # change types
            step = int(md["step"])
            number = md.get("number", None)

            # on new step or number, return/yield group of messages accumulated so far
            # and ensure proper cleanup of memory

            stop = (prev_step is not None and step != prev_step) or (
                prev_number is not None and number != prev_number
            )

            if stop:
                # print(f"yielding {len(messages)} messages")
                yield messages
                for k, m in messages.items():
                    grib = m["grib"]
                    eccodes.codes_release(grib)
                messages = {}

            prev_number = number
            prev_step = step

            # print(f"message {msgcount} mars metadata: {md}")

            # aggregate messages on step, number, assuming they are contiguous

            md["paramId"] = eccodes.codes_get_string(msg, "paramId")
            md["shortName"] = eccodes.codes_get_string(msg, "shortName")

            md["Ni"] = eccodes.codes_get_long(msg, "Ni")
            md["Nj"] = eccodes.codes_get_long(msg, "Nj")

            md["time"] = eccodes.codes_get_long(msg, "time")
            md["date"] = eccodes.codes_get_string(msg, "date")
            md["step"] = step

            sname = md["shortName"]

            # print(f"message {msgcount} step {step} number {number} param {sname}")

            ldate = eccodes.codes_get_long(msg, "date")
            yyyy = math.floor(ldate / 10000)
            mm = math.floor((ldate - (yyyy * 10000)) / 100)
            dd = ldate - (yyyy * 10000) - mm * 100

            md["base_datetime"] = datetime(yyyy, mm, dd, tzinfo=timezone.utc)

            forecast_datetime = (
                datetime(yyyy, mm, dd, tzinfo=timezone.utc)
                + timedelta(minutes=60 * md["time"] / 100)
                + timedelta(minutes=60 * md["step"])
            )

            md["forecast_datetime"] = forecast_datetime

            # decode data
            # get the lats, lons, values
            # md["lats"] = eccodes.codes_get_double_array(msg, "latitudes")
            # print(lats)
            # md["lons"] = eccodes.codes_get_double_array(msg, "longitudes")
            # print(lons)
            global lats
            if lats is None:
                lats = eccodes.codes_get_double_array(msg, "latitudes")
            global lons
            if lons is None:
                lons = eccodes.codes_get_double_array(msg, "longitudes")

            md["values"] = eccodes.codes_get_double_array(msg, "values")
            # print(values)

            md["grib"] = msg  # keep grib open

            assert sname not in messages

            messages[sname] = md

    f.close()


# @thermofeel.timer
def calc_cossza_int(dt, begin, end):

    # print(dt.year, dt.month, dt.day, dt.hour)

    integral = thermofeel.calculate_cos_solar_zenith_angle_integrated(
        lat=lats,
        lon=lons,
        y=dt.year,
        m=dt.month,
        d=dt.day,
        h=dt.hour,
        tbegin=begin,
        tend=end,
        integration_order=2,
    )

    return integral


# @thermofeel.timer
def calc_mrt(messages, cossza):

    step = messages["2t"]["step"]

    factor = 1.0 / (step * 3600.0)

    ssrd = messages["ssrd"]["values"]
    ssr = messages["ssr"]["values"]
    fdir = messages["fdir"]["values"]
    strd = messages["strd"]["values"]
    strr = messages["str"]["values"]

    mrt = thermofeel.calculate_mean_radiant_temperature(
        ssrd=ssrd * factor,
        ssr=ssr * factor,
        fdir=fdir * factor,
        strd=strd * factor,
        strr=strr * factor,
        cossza=cossza * factor,
    )

    return mrt


# @thermofeel.timer
def calc_va(messages):
    u10 = messages["10u"]["values"]
    v10 = messages["10v"]["values"]

    return np.sqrt(u10**2 + v10**2)


# @thermofeel.optnumba_jit
def calc_ehPa_(rh_pc, svp):
    return svp * rh_pc * 0.01  # / 100.0


# @thermofeel.timer
def calc_ehPa(t2m, t2d):
    rh_pc = thermofeel.calculate_relative_humidity_percent(t2m, t2d)
    svp = thermofeel.calculate_saturation_vapour_pressure(t2m)
    ehPa = calc_ehPa_(rh_pc, svp)
    return ehPa


# @thermofeel.timer
def calc_wbgt_in_kelvin(t2m, va, mrt, td):
    wbgt = thermofeel.calculate_wbgt(t_k=t2m, va=va, mrt=mrt, td=td)
    return thermofeel.celsius_to_kelvin(wbgt)


# @thermofeel.timer
def check_messages(msgs):
    # assert "2t" in msgs
    # assert "2d" in msgs
    # assert "10u" in msgs
    # assert "10v" in msgs
    # assert "ssrd" in msgs
    # assert "ssr" in msgs
    # assert "fdir" in msgs
    # assert "str" in msgs
    # assert "strd" in msgs

    assert lats.size == lons.size

    ftime = msgs["2t"]["forecast_datetime"]

    for k, m in msgs.items():
        assert lats.size == m["values"].size
        assert ftime == m["forecast_datetime"]


# @thermofeel.timer
def output_grib(output, msg, paramid, values, missing=None):
    # encode results in GRIB
    grib = msg["grib"]
    handle = eccodes.codes_clone(grib)
    eccodes.codes_set_long(handle, "edition", 2)
    eccodes.codes_set_string(handle, "paramId", paramid)
    eccodes.codes_set_values(handle, values)
    if missing is not None:
        eccodes.codes_set_double(handle, "missingValue", missing)
    eccodes.codes_write(handle, output)
    eccodes.codes_release(handle)


# @thermofeel.timer
def output_gribs(output, msg, cossza, mrt, wbgt, va, td, t2m):
    output_grib(output, msg, "207", va)
    output_grib(output, msg, "168", td)
    output_grib(output, msg, "167", t2m)
    output_grib(output, msg, "214001", cossza)
    output_grib(output, msg, "261001", wbgt, missing=MISSING_VALUE)
    output_grib(output, msg, "261002", mrt)


cossza = None
last_step_end = 0


# @thermofeel.timer
def process_step(msgs, output):

    check_messages(msgs)

    # print(f"loaded {len(msgs)} parameters: {list(msgs.keys())}")

    msg = msgs["2t"]

    step = msg["step"]
    time = msg["time"]
    dt = msgs["2t"]["base_datetime"]

    ftime = int(time / 100)

    step_begin = ftime
    step_end = ftime + step

    print(
        f"dt {dt.date().isoformat()} time {time} step {step} - [{step_begin},{step_end}]"
    )

    global cossza
    global last_step_end
    if cossza is None:
        print(f"[{step_begin},{step_end}]")
        cossza = calc_cossza_int(dt=dt, begin=step_begin, end=step_end)
    else:
        print(f"[{last_step_end},{step_end}]")
        cossza += calc_cossza_int(dt=dt, begin=last_step_end, end=step_end)

    last_step_end = step_end
    td = msgs["2d"]["values"]
    t2m = msg["values"]
    mrt = calc_mrt(messages=msgs, cossza=cossza)
    va = calc_va(messages=msgs)
    utci = calc_wbgt_in_kelvin(messages=msgs, t2m=t2m, va=va, mrt=mrt, td=td)

    output_gribs(
        output=output, msg=msg, cossza=cossza, mrt=mrt, utci=utci, td=td, t2m=t2m, va=va
    )


def main():

    print(f"Thermofeel version: {thermofeel.__version__}")
    print(f"Python version: {sys.version}")
    print(f"Numpy version: {np.version.version}")
    np.show_config()

    output = open(sys.argv[2], "wb")

    print("----------------------------------------")
    for msgs in decode_grib(sys.argv[1]):
        process_step(msgs, output)
        print("----------------------------------------")


if __name__ == "__main__":
    sys.exit(main())
