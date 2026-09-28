# -*- coding: utf-8 -*-
"""Генератор тестового DXF: участок, здание, дорога, тротуар,
подземные сети и существующие деревья. Нужен, пока нет данных пилота."""
import ezdxf


def main(path="data/sample.dxf"):
    doc = ezdxf.new("R2010", setup=False)
    msp = doc.modelspace()

    def layer(name, color):
        if name not in doc.layers:
            doc.layers.add(name, color=color)
        return name

    msp.add_lwpolyline([(0, 0), (120, 0), (120, 60), (0, 60)], close=True,
                       dxfattribs={"layer": layer("ГРАНИЦА_РАБОТ", 1)})
    msp.add_lwpolyline([(10, 40), (45, 40), (45, 56), (10, 56)], close=True,
                       dxfattribs={"layer": layer("ЗДАНИЯ", 7)})
    msp.add_lwpolyline([(0, 8), (120, 8)],
                       dxfattribs={"layer": layer("ПРОЕЗЖАЯ_ЧАСТЬ", 5)})
    msp.add_lwpolyline([(0, 14), (120, 14)],
                       dxfattribs={"layer": "ПРОЕЗЖАЯ_ЧАСТЬ"})
    msp.add_lwpolyline([(0, 20), (120, 20)],
                       dxfattribs={"layer": layer("ТРОТУАР", 8)})
    msp.add_lwpolyline([(0, 23), (120, 23)], dxfattribs={"layer": "ТРОТУАР"})
    msp.add_lwpolyline([(0, 30), (120, 30)],
                       dxfattribs={"layer": layer("ВОДОПРОВОД_В1", 4)})
    msp.add_lwpolyline([(0, 34), (60, 34), (60, 60)],
                       dxfattribs={"layer": layer("ГАЗОПРОВОД", 2)})
    msp.add_lwpolyline([(0, 26), (120, 26)],
                       dxfattribs={"layer": layer("КАБЕЛЬ_СВЯЗИ", 6)})
    msp.add_lwpolyline([(80, 0), (80, 60)],
                       dxfattribs={"layer": layer("КАНАЛИЗАЦИЯ_К1", 3)})
    for x in (15, 40, 65, 90, 110):
        msp.add_circle((x, 17), 0.3,
                       dxfattribs={"layer": layer("ОПОРЫ_ОСВЕЩЕНИЯ", 30)})
    for x, y in ((55, 35), (95, 45), (105, 30), (68, 50)):
        msp.add_circle((x, y), 3.0,
                       dxfattribs={"layer": layer("СУЩ_ДЕРЕВЬЯ", 3)})

    doc.saveas(path)
    print("Готов тестовый чертёж:", path)


if __name__ == "__main__":
    main()
