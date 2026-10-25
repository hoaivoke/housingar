"""Compare exported geometry with measured multi-view object extent."""
import numpy as np


def height_evidence(vertices,up,reference=0.):
    points=np.asarray(vertices);height=points@up
    return {'vertices':len(points),'above_reference':int((height>reference).sum()),
        'above_reference_plus_5cm':int((height>reference+.05).sum()),
        'min_height_m':float(height.min()),'max_height_m':float(height.max())}


def geometry_completeness(vertices,extent,components=None):
    points=np.asarray(vertices)@extent.basis
    low,high=points.min(0),points.max(0)
    expected=extent.high-extent.low;actual=high-low
    fraction=actual/np.maximum(expected,1e-9)
    shortfall=np.maximum(expected-actual,0.)
    missing=((fraction<.8)&(shortfall>.03))|(shortfall>.05)
    names=['width','height','depth']
    warnings=[f'Object mesh {names[i]} covers only {fraction[i]:.0%} of the refined depth extent; object geometry is incomplete' for i in range(3) if missing[i]]
    component_count=int((components or {}).get('retained',0))
    fragmented=component_count>200
    if fragmented:warnings.append(f'{component_count} disconnected depth-supported mesh components remain; capture closer views of thin parts')
    return {'refined_dimensions_m':expected.tolist(),'mesh_dimensions_in_refined_frame_m':actual.tolist(),
        'axis_coverage_fraction':fraction.tolist(),'height_coverage_fraction':float(fraction[1]),
        'footprint_coverage_fraction':float(min(fraction[0],fraction[2])),
        'shortfall_m':shortfall.tolist(),'minimum_axis_coverage':.8,'absolute_shortfall_threshold_m':.03,'maximum_absolute_shortfall_m':.05,
        'validated_component_count':component_count,'maximum_good_quality_components':200,'fragmented_geometry':fragmented,
        'low_quality':bool(missing.any() or fragmented),'warnings':warnings}
