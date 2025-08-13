from django import template

register = template.Library()

@register.filter(name='as_widget')
def as_widget(field):
    return field.as_widget(attrs={'class': 'form-control'})
