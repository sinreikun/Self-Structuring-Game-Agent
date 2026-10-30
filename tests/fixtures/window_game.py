"""A Windows CI test fixture, never used as the product's game policy."""
import tkinter as tk

root = tk.Tk()
root.title('SelfStruct I/O Test')
root.geometry('400x300+100+100')
canvas = tk.Canvas(root, width=400, height=300, bg='#203040')
canvas.pack(fill='both', expand=True)
counter = 0
def key(event):
    global counter
    counter += 1
    canvas.configure(bg='#c04020' if counter % 2 else '#203040')
root.bind('<KeyPress>', key)
root.after(200, root.focus_force)
root.mainloop()
