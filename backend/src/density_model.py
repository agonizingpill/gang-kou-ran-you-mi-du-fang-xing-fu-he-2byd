from .constants import GRADES


def within_spec(grade, density, viscosity, sulfur):
    """Hard grade limits; the KDE threshold is a separate similarity gate."""
    g = GRADES[grade]
    return (
        g['density_min'] <= float(density) <= g['density_max']
        and g['viscosity_min'] <= float(viscosity) <= g['viscosity_max']
        and float(sulfur) <= g['sulfur_max']
    )
